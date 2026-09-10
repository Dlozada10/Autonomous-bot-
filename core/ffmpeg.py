"""Locate the ffmpeg binary.

Uses the pip-vendored static build so the project runs on a bare machine with
no system ffmpeg, then falls back to whatever is on PATH.
"""
from __future__ import annotations

import shutil

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # pragma: no cover - only when the wheel is missing
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
