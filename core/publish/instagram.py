"""Instagram Reels upload via the Graph API.

Instagram will not accept a file upload - it fetches the video from a URL you
supply. That means rendered files must be reachable over public HTTPS, so set
PUBLIC_MEDIA_BASE_URL to an S3/R2/nginx origin serving the out/ directory.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

API = "https://graph.facebook.com/v21.0"


def upload(video: dict, script: dict, cfg: Any) -> tuple[str, str]:
    from . import caption_for

    user_id = cfg.require("IG_USER_ID")
    token = cfg.require("IG_ACCESS_TOKEN")
    base = cfg.require("PUBLIC_MEDIA_BASE_URL").rstrip("/")
    video_url = f"{base}/{os.path.basename(video['path'])}"

    create = requests.post(
        f"{API}/{user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption_for(script, 2200),
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=90,
    )
    create.raise_for_status()
    container = create.json()["id"]

    # Instagram must download and transcode before the container is publishable.
    for _ in range(40):
        time.sleep(6)
        check = requests.get(
            f"{API}/{container}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=60,
        )
        check.raise_for_status()
        code = check.json().get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"instagram failed to process: {check.json().get('status')}")
    else:
        raise RuntimeError(f"instagram container {container} never finished")

    publish = requests.post(
        f"{API}/{user_id}/media_publish",
        data={"creation_id": container, "access_token": token}, timeout=90,
    )
    publish.raise_for_status()
    media_id = publish.json()["id"]
    return media_id, f"https://www.instagram.com/reel/{media_id}/"
