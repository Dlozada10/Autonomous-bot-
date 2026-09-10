"""TikTok upload via the Content Posting API.

Requires an approved developer app with the video.publish scope. Until the
app passes TikTok's audit, posts land as private drafts in the creator's
inbox regardless of what you send - budget for that review in your timeline.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

API = "https://open.tiktokapis.com/v2"
CHUNK = 10 * 1024 * 1024


def upload(video: dict, script: dict, cfg: Any) -> tuple[str, str]:
    from . import caption_for

    token = cfg.require("TIKTOK_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json; charset=UTF-8"}

    size = os.path.getsize(video["path"])
    # TikTok requires a single chunk for files under its minimum chunk size.
    chunk_size = size if size <= CHUNK else CHUNK
    total_chunks = 1 if size <= CHUNK else (size + chunk_size - 1) // chunk_size

    init = requests.post(
        f"{API}/post/publish/video/init/",
        headers=headers,
        json={
            "post_info": {
                "title": caption_for(script, 2100),
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunks,
            },
        },
        timeout=60,
    )
    init.raise_for_status()
    payload = init.json()["data"]
    publish_id, upload_url = payload["publish_id"], payload["upload_url"]

    with open(video["path"], "rb") as fh:
        for index in range(total_chunks):
            start = index * chunk_size
            blob = fh.read(chunk_size)
            end = start + len(blob) - 1
            put = requests.put(
                upload_url,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(blob)),
                },
                data=blob, timeout=300,
            )
            put.raise_for_status()

    # Processing is asynchronous; poll until TikTok commits or rejects it.
    for _ in range(40):
        time.sleep(6)
        status = requests.post(
            f"{API}/post/publish/status/fetch/",
            headers=headers, json={"publish_id": publish_id}, timeout=60,
        )
        status.raise_for_status()
        data = status.json()["data"]
        state = data.get("status")
        if state in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}:
            return publish_id, data.get("public_post_url", "") or "https://www.tiktok.com/"
        if state == "FAILED":
            raise RuntimeError(f"tiktok rejected the post: {data.get('fail_reason')}")

    raise RuntimeError(f"tiktok processing timed out (publish_id={publish_id})")
