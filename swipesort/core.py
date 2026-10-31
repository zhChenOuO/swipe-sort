"""掃描媒體、記錄決定、搬進/搬出垃圾桶。不碰 HTTP。"""

from __future__ import annotations

import bisect
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif",
}
VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".mts", ".m2ts", ".wmv",
}
TRASH_DIR = ".swipe-trash"
KEEP, TRASH, MOVE = "keep", "trash", "move"


def media_kind(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def skipped(name: str) -> bool:
    # . 開頭:隱藏檔、macOS 的 ._xxx、我們自己的垃圾桶
    # @ / # 開頭:Synology 的 @eaDir、#recycle、#snapshot
    return name[:1] in (".", "@", "#")


class SwipeError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    path: str  # 相對於根目錄,一律用 /
    kind: str
    size: int
    mtime: float

    def as_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "size": self.size, "mtime": self.mtime}


def folder_of(rel: str) -> str:
    parent = str(PurePosixPath(rel).parent)
    return "" if parent == "." else parent


def in_folder(rel: str, folder: str) -> bool:
    return not folder or rel.startswith(folder + "/")


class Library:
    def __init__(self, root: Path | str, db_path: Path | str, scan: bool = True):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise SwipeError(f"根目錄不存在: {self.root}")
        self.trash_root = self.root / TRASH_DIR
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS decisions ("
            " path TEXT PRIMARY KEY, decision TEXT NOT NULL,"
            " trash_path TEXT, size INTEGER, seq INTEGER NOT NULL, at REAL NOT NULL)"
            # trash_path:刪除時是垃圾桶裡的相對路徑,搬移時是搬過去的新路徑
        )
        self._db.commit()
        self._paths: list[str] = []
        self._entries: dict[str, Entry] = {}
        # 搬過去的新路徑也算處理過,不然掃描時會在目標資料夾再出現一次
        self._decided: set[str] = set()
        for path, decision, dest in self._db.execute("SELECT path, decision, trash_path FROM decisions"):
            self._decided.add(path)
            if decision == MOVE:
                self._decided.add(dest)
        self.scanned_at = 0.0
        self.scanning = False
        if scan:
            self.scan()

    # ---------- 路徑 ----------

    def _abs(self, rel: str, trash: bool = False) -> Path:
        """把相對路徑轉成絕對路徑,擋掉 .. 或跳出根目錄的路徑。"""
        if not rel or rel.startswith("/") or "\\" in rel:
            raise SwipeError(f"不合法的路徑: {rel!r}")
        parts = PurePosixPath(rel).parts
        if any(p in ("", ".", "..") for p in parts):
            raise SwipeError(f"不合法的路徑: {rel!r}")
        base = self.trash_root if trash else self.root
        p = (base / Path(*parts)).resolve()
        if base.resolve() not in p.parents:
            raise SwipeError(f"路徑跳出根目錄: {rel!r}")
        if not trash and p.relative_to(self.root).parts[0] == TRASH_DIR:
            raise SwipeError(f"不合法的路徑: {rel!r}")
        return p

    def file_path(self, rel: str, trash: bool = False) -> Path:
        p = self._abs(rel, trash)
        if not p.is_file() or media_kind(p.name) is None:
            raise SwipeError(f"找不到檔案: {rel}")
        return p

    # ---------- 掃描 ----------

    def _entry(self, rel: str, p: Path) -> Entry | None:
        kind = media_kind(p.name)
        if kind is None:
            return None
        try:
            st = p.stat()
        except OSError:
            return None
        return Entry(rel, kind, st.st_size, st.st_mtime)

    def scan(self) -> int:
        self.scanning = True
        try:
            return self._scan()
        finally:
            self.scanning = False

    def _scan(self) -> int:
        entries: dict[str, Entry] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not skipped(d))
            reldir = os.path.relpath(dirpath, self.root)
            for name in filenames:
                if skipped(name):
                    continue
                rel = name if reldir == "." else f"{reldir}/{name}".replace(os.sep, "/")
                e = self._entry(rel, Path(dirpath) / name)
                if e:
                    entries[rel] = e
        with self._lock:
            self._entries = entries
            self._paths = sorted(entries)
            self.scanned_at = time.time()
        return len(entries)

    def _add(self, rel: str) -> None:
        e = self._entry(rel, self.root / rel)
        if e is None:
            return
        if rel not in self._entries:
            bisect.insort(self._paths, rel)
        self._entries[rel] = e

    # ---------- 查詢 ----------

    def folders(self) -> list[dict]:
        """每個資料夾(含子資料夾)的總數與待處理數。"""
        with self._lock:
            total: dict[str, int] = {}
            pending: dict[str, int] = {}
            for rel in self._paths:
                undecided = rel not in self._decided
                folder = folder_of(rel)
                chain = [""]
                if folder:
                    parts = folder.split("/")
                    chain += ["/".join(parts[: i + 1]) for i in range(len(parts))]
                for f in chain:
                    total[f] = total.get(f, 0) + 1
                    if undecided:
                        pending[f] = pending.get(f, 0) + 1
            return [{"path": f, "total": total[f], "pending": pending.get(f, 0)} for f in sorted(total)]

    def queue(self, folder: str = "", after: str = "", limit: int = 30) -> list[dict]:
        """folder 底下(含子資料夾)還沒決定的檔案,依路徑排序,從 after 之後開始。"""
        folder = folder.strip("/")
        with self._lock:
            start = bisect.bisect_right(self._paths, after) if after else 0
            if folder:
                start = max(start, bisect.bisect_left(self._paths, folder + "/"))
            out = []
            for rel in self._paths[start:]:
                if not in_folder(rel, folder):
                    break
                if rel not in self._decided:
                    out.append(self._entries[rel].as_dict())
                    if len(out) >= limit:
                        break
            return out

    def stats(self) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM decisions WHERE decision = ?", (TRASH,)
            ).fetchone()
            keep = self._db.execute(
                "SELECT COUNT(*) FROM decisions WHERE decision = ?", (KEEP,)
            ).fetchone()[0]
            return {
                "total": len(self._paths),
                "pending": sum(1 for r in self._paths if r not in self._decided),
                "kept": keep,
                "trashed": row[0],
                "trash_bytes": row[1],
                "scanned_at": self.scanned_at,
                "scanning": self.scanning,
            }

    # ---------- 決定 / 還原 ----------

    def _next_seq(self) -> int:
        return self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM decisions").fetchone()[0]


    def _folder(self, folder: str) -> str:
        """檢查搬移的目標資料夾,回傳正規化後的相對路徑。"""
        folder = folder.strip().strip("/")
        parts = PurePosixPath(folder).parts if folder else ()
        if "\\" in folder or any(p in (".", "..") or skipped(p) for p in parts):
            raise SwipeError(f"不合法的資料夾: {folder!r}")
        if folder:
            p = (self.root / Path(*parts)).resolve()
            if self.root not in p.parents:
                raise SwipeError(f"資料夾跳出根目錄: {folder!r}")
            if p.exists() and not p.is_dir():
                raise SwipeError(f"不是資料夾: {folder}")
        return "/".join(parts)

    def _free_name(self, base: Path, rel: str) -> str:
        cand = rel
        stem, ext = os.path.splitext(rel)
        n = 1
        while (base / cand).exists():
            cand = f"{stem} ({n}){ext}"
            n += 1
        return cand

    def decide(self, rel: str, decision: str, target: str | None = None) -> dict:
        if decision not in (KEEP, TRASH, MOVE):
            raise SwipeError(f"未知的決定: {decision!r}")
        with self._lock:
            src = self.file_path(rel)
            if rel in self._decided:
                raise SwipeError(f"已經處理過: {rel}")
            size = src.stat().st_size
            trash_rel = None
            if decision == TRASH:
                trash_rel = self._move_to_trash(rel, src)
            elif decision == MOVE:
                folder = self._folder(target or "")
                if folder == folder_of(rel):
                    raise SwipeError("已經在這個資料夾裡了")
                name = PurePosixPath(rel).name
                trash_rel = self._free_name(self.root, f"{folder}/{name}" if folder else name)
                dst = self.root / trash_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                self._decided.add(trash_rel)
                self._add(trash_rel)
            self._db.execute(
                "INSERT INTO decisions (path, decision, trash_path, size, seq, at) VALUES (?, ?, ?, ?, ?, ?)",
                (rel, decision, trash_rel, size, self._next_seq(), time.time()),
            )
            self._db.commit()
            self._decided.add(rel)
            return {"path": rel, "decision": decision, "trash_path": trash_rel}

    def _move_to_trash(self, rel: str, src: Path) -> str:
        trash_rel = self._free_name(self.trash_root, rel)
        dst = self.trash_root / trash_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return trash_rel

    def decide_all(self, folder: str, decision: str) -> dict:
        """folder(含子資料夾)裡還沒決定的檔案全部保留或全部刪除。
        同一批共用一個 seq,undo 會整批撤回。"""
        if decision not in (KEEP, TRASH):
            raise SwipeError(f"未知的決定: {decision!r}")
        folder = folder.strip("/")
        with self._lock:
            start = bisect.bisect_left(self._paths, folder + "/") if folder else 0
            pending = []
            for rel in self._paths[start:]:
                if not in_folder(rel, folder):
                    break
                if rel not in self._decided:
                    pending.append(rel)
            seq, now = self._next_seq(), time.time()
            done, failed = [], []
            try:
                for rel in pending:
                    trash_rel = None
                    if decision == TRASH:
                        try:
                            trash_rel = self._move_to_trash(rel, self.file_path(rel))
                        except (OSError, SwipeError) as e:
                            failed.append(f"{rel}: {e}")
                            continue
                    done.append((rel, decision, trash_rel, self._entries[rel].size, seq, now))
            finally:
                # 搬到一半出錯也要把已經搬走的記下來,不然 undo 找不回來
                self._db.executemany(
                    "INSERT INTO decisions (path, decision, trash_path, size, seq, at) VALUES (?, ?, ?, ?, ?, ?)",
                    done,
                )
                self._db.commit()
                self._decided.update(r[0] for r in done)
            return {"count": len(done), "failed": failed}

    def _revert(self, rel: str, decision: str, trash_rel: str | None) -> None:
        if decision == TRASH:
            src = self._abs(trash_rel, trash=True)
            dst = self._abs(rel)
            if not src.is_file():
                raise SwipeError(f"垃圾桶裡找不到: {trash_rel}")
            if dst.exists():
                raise SwipeError(f"原位置已經有檔案,沒有覆蓋: {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            self._prune_trash_dirs(src.parent)
        elif decision == MOVE:
            src = self._abs(trash_rel)
            dst = self._abs(rel)
            if not src.is_file():
                raise SwipeError(f"搬過去的檔案不見了: {trash_rel}")
            if dst.exists():
                raise SwipeError(f"原位置已經有檔案,沒有覆蓋: {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            self._decided.discard(trash_rel)
            if self._entries.pop(trash_rel, None):
                self._paths.remove(trash_rel)
        self._db.execute("DELETE FROM decisions WHERE path = ?", (rel,))
        self._db.commit()
        self._decided.discard(rel)
        self._add(rel)

    def undo(self) -> dict | None:
        """撤回最後一個決定(保留或刪除都算),回傳那個檔案;整批的會整批撤回。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT path, decision, trash_path FROM decisions"
                " WHERE seq = (SELECT MAX(seq) FROM decisions)"
            ).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                failed = []
                for rel, decision, trash_rel in rows:
                    try:
                        self._revert(rel, decision, trash_rel)
                    except SwipeError as e:
                        failed.append(str(e))
                return {"undone": rows[0][1], "batch": len(rows) - len(failed), "failed": failed}
            rel, decision, trash_rel = rows[0]
            self._revert(rel, decision, trash_rel)
            e = self._entries.get(rel)
            return {"undone": decision, **(e.as_dict() if e else {"path": rel})}

    def trash_items(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT path, trash_path, size, at FROM decisions WHERE decision = ? ORDER BY seq DESC",
                (TRASH,),
            ).fetchall()
            return [
                {"path": p, "trash_path": t, "size": s, "at": at, "kind": media_kind(p)}
                for p, t, s, at in rows
            ]

    def restore(self, rel: str) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT trash_path FROM decisions WHERE path = ? AND decision = ?", (rel, TRASH)
            ).fetchone()
            if row is None:
                raise SwipeError(f"不在垃圾桶裡: {rel}")
            self._revert(rel, TRASH, row[0])
            return {"path": rel}

    def restore_all(self) -> dict:
        restored, failed = 0, []
        for item in self.trash_items():
            try:
                self.restore(item["path"])
                restored += 1
            except SwipeError as e:
                failed.append(str(e))
        return {"restored": restored, "failed": failed}

    def reset_kept(self, folder: str = "") -> int:
        """把「保留」清掉,讓那些檔案重新出現在佇列裡。"""
        folder = folder.strip("/")
        with self._lock:
            rows = [r[0] for r in self._db.execute(
                "SELECT path FROM decisions WHERE decision = ?", (KEEP,)
            ) if in_folder(r[0], folder)]
            self._db.executemany("DELETE FROM decisions WHERE path = ?", [(r,) for r in rows])
            self._db.commit()
            self._decided.difference_update(rows)
            return len(rows)

    def purge(self) -> dict:
        """永久刪除垃圾桶裡的檔案。這一步不能還原。"""
        with self._lock:
            deleted, freed, failed = 0, 0, []
            for item in self.trash_items():
                try:
                    p = self._abs(item["trash_path"], trash=True)
                    if p.exists():
                        freed += p.stat().st_size
                        p.unlink()
                    self._prune_trash_dirs(p.parent)
                except (OSError, SwipeError) as e:
                    failed.append(f"{item['path']}: {e}")
                    continue
                self._db.execute("DELETE FROM decisions WHERE path = ?", (item["path"],))
                self._decided.discard(item["path"])
                self._entries.pop(item["path"], None)
                deleted += 1
            self._db.commit()
            self._paths = sorted(self._entries)
            return {"deleted": deleted, "freed": freed, "failed": failed}

    def _prune_trash_dirs(self, d: Path) -> None:
        while d != self.trash_root and self.trash_root in d.parents:
            try:
                d.rmdir()
            except OSError:
                return
            d = d.parent
