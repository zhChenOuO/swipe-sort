"""HTTP 伺服器:只用標準函式庫。"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import Library, SwipeError, media_kind
from . import transcode
from .preview import preview

STATIC = Path(__file__).parent / "static"
CHUNK = 1 << 20
for ext, mime in {".heic": "image/heic", ".heif": "image/heif", ".mkv": "video/x-matroska",
                  ".mts": "video/mp2t", ".m2ts": "video/mp2t", ".webp": "image/webp",
                  ".avif": "image/avif", ".m4v": "video/mp4"}.items():
    mimetypes.add_type(mime, ext)


class App:
    def __init__(self, lib: Library, data_dir: Path, password: str | None):
        self.lib = lib
        self.cache_dir = data_dir / "cache"
        self.password = password
        self.scanning = threading.Lock()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "swipe-sort"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("SWIPE_LOG"):
                super().log_message(fmt, *args)

        # ---------- 回應 ----------

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, msg, status=HTTPStatus.BAD_REQUEST):
            self._json({"error": msg}, status)

        def _send_file(self, path: Path, ctype: str | None = None, cache: str = "no-cache"):
            size = path.stat().st_size
            ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:
                    start = max(size - int(m[2]), 0)
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(HTTPStatus.OK)
            length = end - start + 1
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as f:
                f.seek(start)
                while length > 0:
                    buf = f.read(min(CHUNK, length))
                    if not buf:
                        break
                    try:
                        self.wfile.write(buf)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    length -= len(buf)

        # ---------- 認證 ----------

        def _authorized(self) -> bool:
            if not app.password:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                except ValueError:
                    return False
                return hmac.compare_digest(pw, app.password)
            return False

        def _challenge(self):
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="swipe-sort"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---------- 路由 ----------

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._authorized():
                return self._challenge()
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    return self._send_file(STATIC / "index.html", "text/html; charset=utf-8")
                if url.path.startswith("/static/"):
                    p = (STATIC / url.path[len("/static/"):]).resolve()
                    if STATIC.resolve() not in p.parents or not p.is_file():
                        return self._error("not found", HTTPStatus.NOT_FOUND)
                    return self._send_file(p)
                if url.path == "/api/folders":
                    return self._json({"folders": app.lib.folders(), "stats": app.lib.stats()})
                if url.path == "/api/queue":
                    limit = max(1, min(int(q.get("limit", 30)), 200))
                    return self._json({"items": app.lib.queue(q.get("folder", ""), q.get("after", ""), limit)})
                if url.path == "/api/list":
                    limit = max(1, min(int(q.get("limit", 100)), 500))
                    return self._json({"items": app.lib.listing(q.get("folder", ""), q.get("after", ""), limit)})
                if url.path == "/api/trash":
                    return self._json({"items": app.lib.trash_items(), "stats": app.lib.stats()})
                if url.path in ("/api/file", "/api/preview", "/api/video"):
                    trash = q.get("trash") == "1"
                    src = app.lib.file_path(q.get("path", ""), trash=trash)
                    if url.path == "/api/video" and transcode.needs_transcode(src):
                        out = transcode.video(src, app.cache_dir)
                        if not out:
                            return self._error("這個影片轉不了檔", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                        return self._send_file(out, "video/mp4", "private, max-age=86400")
                    if url.path == "/api/preview":
                        size = max(64, min(int(q.get("size", 1600)), 4096))
                        out = preview(src, app.cache_dir, size)
                        if out:
                            return self._send_file(out, "image/jpeg", "private, max-age=86400")
                        if media_kind(src.name) == "video":
                            return self._error("無法產生影片縮圖", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                    return self._send_file(src)
                return self._error("not found", HTTPStatus.NOT_FOUND)
            except SwipeError as e:
                return self._error(str(e), HTTPStatus.NOT_FOUND)
            except ValueError as e:
                return self._error(str(e))

        def do_POST(self):
            if not self._authorized():
                return self._challenge()
            url = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._error("JSON 格式錯誤")
            lib = app.lib
            try:
                if url.path == "/api/decide":
                    return self._json(lib.decide(body.get("path", ""), body.get("decision", ""), body.get("target")))
                if url.path == "/api/decide-all":
                    return self._json(lib.decide_all(body.get("folder", ""), body.get("decision", "")))
                if url.path == "/api/undo":
                    return self._json({"item": lib.undo()})
                if url.path == "/api/restore":
                    if body.get("all"):
                        return self._json(lib.restore_all())
                    return self._json(lib.restore(body.get("path", "")))
                if url.path == "/api/purge":
                    if body.get("confirm") != "DELETE":
                        return self._error("需要 confirm=DELETE")
                    return self._json(lib.purge())
                if url.path == "/api/reset-kept":
                    return self._json({"reset": lib.reset_kept(body.get("folder", ""))})
                if url.path == "/api/rescan":
                    if not app.scanning.acquire(blocking=False):
                        return self._error("正在掃描中", HTTPStatus.CONFLICT)
                    try:
                        return self._json({"total": lib.scan()})
                    finally:
                        app.scanning.release()
                return self._error("not found", HTTPStatus.NOT_FOUND)
            except SwipeError as e:
                return self._error(str(e), HTTPStatus.CONFLICT)

    return Handler


def main(argv: list[str] | None = None) -> None:
    env = os.environ
    ap = argparse.ArgumentParser(prog="swipe-sort", description="左滑刪除、右滑保留,整理資料夾裡的圖片和影片")
    ap.add_argument("--root", default=env.get("SWIPE_ROOT"), help="要整理的父資料夾(會掃描所有子資料夾)")
    ap.add_argument("--data", default=env.get("SWIPE_DATA", "./data"), help="存放資料庫和預覽快取的資料夾")
    ap.add_argument("--host", default=env.get("SWIPE_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(env.get("SWIPE_PORT", 8080)))
    args = ap.parse_args(argv)
    if not args.root:
        ap.error("請用 --root 或 SWIPE_ROOT 指定要整理的資料夾")

    data = Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    try:
        lib = Library(args.root, data / "swipe.db", scan=False)
    except SwipeError as e:
        sys.exit(str(e))
    app = App(lib, data, env.get("SWIPE_PASSWORD") or None)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    srv.daemon_threads = True

    def first_scan():
        with app.scanning:
            n = lib.scan()
        print(f"swipe-sort: 掃描完成,共 {n} 個檔案", flush=True)

    threading.Thread(target=first_scan, daemon=True).start()
    print(f"swipe-sort: {lib.root} 掃描中, http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
