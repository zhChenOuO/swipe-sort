"""產生縮小的 JPEG 預覽。圖片用 Pillow,影片用 ffmpeg 抓一張畫面。
做不出來時回傳 None,由呼叫端決定怎麼辦。"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from . import transcode
from .core import media_kind

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

# 瀏覽器本來就能顯示、又不用縮的格式
PASSTHROUGH = {".gif"}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def preview(src: Path, cache_dir: Path, size: int) -> Path | None:
    is_video = media_kind(src.name) == "video"
    if not is_video and (Image is None or src.suffix.lower() in PASSTHROUGH):
        return None
    st = src.stat()
    key = hashlib.sha1(f"{src}|{st.st_size}|{st.st_mtime_ns}|{size}".encode()).hexdigest()
    out = cache_dir / key[:2] / f"{key}.jpg"
    if out.exists():
        return out
    with _lock_for(key):
        if out.exists():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        if is_video:
            return out if transcode.poster(src, out, size) else None
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((size, size))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                tmp = out.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=85)
                tmp.replace(out)
        except Exception:
            return None
    return out
