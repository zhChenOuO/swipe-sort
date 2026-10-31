"""用 ffmpeg 把瀏覽器不能播的影片轉成 H.264 MP4 預覽,放在快取裡。原檔不動。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

# 瀏覽器可以直接播的外殼,其他的一律轉
NATIVE = {".mp4", ".m4v", ".mov", ".webm"}
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
# 預覽只轉前面這幾秒,整部電影轉完太久
MAX_SECONDS = int(os.environ.get("SWIPE_VIDEO_SECONDS", 600))

_slots = threading.Semaphore(int(os.environ.get("SWIPE_TRANSCODE_JOBS", 2)))
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def needs_transcode(src: Path) -> bool:
    return src.suffix.lower() not in NATIVE


def _probe(src: Path) -> dict:
    if not FFPROBE:
        return {}
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt", "-of", "json", str(src)],
            capture_output=True, timeout=60,
        )
        streams = json.loads(r.stdout or b"{}").get("streams", [])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    info: dict = {}
    for s in streams:
        kind = s.get("codec_type")
        if kind in ("video", "audio") and kind not in info:
            info[kind] = s
    return info


def _commands(src: Path, out: Path, info: dict) -> list[list[str]]:
    v = info.get("video", {})
    a = info.get("audio", {})
    base = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-t", str(MAX_SECONDS),
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if not a:
        audio = ["-an"]
    elif a.get("codec_name") == "aac":
        audio = ["-c:a", "copy"]
    else:
        audio = ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    tail = ["-movflags", "+faststart", "-f", "mp4", str(out)]
    cmds = []
    # 已經是一般的 H.264,只換外殼,幾乎不花時間
    if v.get("codec_name") == "h264" and v.get("pix_fmt") in ("yuv420p", "yuvj420p"):
        cmds.append(base + ["-c:v", "copy"] + audio + tail)
    vf = ["-vf", "scale=-2:'min(720,trunc(ih/2)*2)',format=yuv420p"]
    # NVIDIA 顯卡:解碼和編碼都交給 GPU;沒有的話這行會失敗,改用下一行的 CPU 編碼
    gpu = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-hwaccel", "cuda"] + base[5:]
    cmds.append(gpu + vf + ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28", "-b:v", "0"] + audio + tail)
    cmds.append(base + vf + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"] + audio + tail)
    return cmds


def video(src: Path, cache_dir: Path) -> Path | None:
    """回傳可以給瀏覽器播的 MP4;轉不了就回傳 None。第一次會等轉檔完成。"""
    if not FFMPEG:
        return None
    st = src.stat()
    key = hashlib.sha1(f"{src}|{st.st_size}|{st.st_mtime_ns}|{MAX_SECONDS}".encode()).hexdigest()
    out = cache_dir / "video" / key[:2] / f"{key}.mp4"
    failed = out.with_suffix(".fail")
    if out.exists():
        return out
    if failed.exists():
        return None
    with _lock_for(key):
        if out.exists():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part.mp4")
        err = b""
        with _slots:
            for cmd in _commands(src, tmp, _probe(src)):
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=1800)
                except (OSError, subprocess.TimeoutExpired) as e:
                    err = str(e).encode()
                    continue
                if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(out)
                    return out
                err = r.stderr
        tmp.unlink(missing_ok=True)
        failed.write_bytes(err[-4000:])
        return None


def poster(src: Path, out: Path, size: int) -> bool:
    """從影片抓一張畫面當縮圖。"""
    if not FFMPEG:
        return False
    tmp = out.with_suffix(".tmp.jpg")
    vf = f"scale={size}:{size}:force_original_aspect_ratio=decrease"
    for ss in ("1", "0"):
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", ss, "-i", str(src),
                 "-frames:v", "1", "-vf", vf, "-q:v", "4", str(tmp)],
                capture_output=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return True
    tmp.unlink(missing_ok=True)
    return False
