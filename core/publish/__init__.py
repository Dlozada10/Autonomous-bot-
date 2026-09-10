"""Platform publishers.

Every publisher exposes upload(video_row, script, cfg) -> (remote_id, url)
and raises on failure. The caller records the outcome either way, so a
platform being down never loses the rendered file.
"""
from __future__ import annotations

from typing import Any, Callable

from . import instagram, tiktok, youtube

PUBLISHERS: dict[str, Callable[..., tuple[str, str]]] = {
    "youtube": youtube.upload,
    "tiktok": tiktok.upload,
    "instagram": instagram.upload,
}


def caption_for(script: Any, limit: int = 2100) -> str:
    """Shared description/caption text with hashtags appended."""
    tags = " ".join(f"#{t.lstrip('#')}" for t in (script.get("hashtags") or []))
    body = f"{script.get('description', '')}\n\n{tags}".strip()
    return body[:limit]
