"""YouTube Shorts upload via the Data API v3.

Quota note: an upload costs 1600 units against a default 10,000/day quota,
so roughly six uploads per day before you must request more.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _service(cfg: Any):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = Path(cfg.env("YOUTUBE_TOKEN", "secrets/youtube_token.json"))
    if not token_path.exists():
        raise RuntimeError(
            f"{token_path} not found. Run: python run.py auth youtube"
        )
    creds = Credentials.from_authorized_user_info(
        json.loads(token_path.read_text()), SCOPES
    )
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise RuntimeError("YouTube credentials are invalid; re-run auth.")
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def authorize(cfg: Any) -> None:
    """One-time OAuth consent. Run on a machine with a browser, then copy
    the token file to wherever the pipeline actually runs."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = Path(cfg.env("YOUTUBE_CLIENT_SECRETS", "secrets/youtube_client_secret.json"))
    if not secrets.exists():
        raise RuntimeError(
            f"{secrets} not found. Create an OAuth desktop client in Google Cloud "
            "Console (YouTube Data API v3 enabled) and download the JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = Path(cfg.env("YOUTUBE_TOKEN", "secrets/youtube_token.json"))
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"saved {token_path}")


def upload(video: dict, script: dict, cfg: Any) -> tuple[str, str]:
    from googleapiclient.http import MediaFileUpload

    from . import caption_for

    service = _service(cfg)
    title = script.get("title", video["title"])[:95]
    # #Shorts in the title is the most reliable Shorts classification signal
    # alongside the vertical aspect ratio and sub-60s duration.
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"

    body = {
        "snippet": {
            "title": title,
            "description": caption_for(script, 4900),
            "tags": [t.lstrip("#") for t in (script.get("hashtags") or [])],
            "categoryId": str(cfg.get("publish.platforms.youtube.category_id", "28")),
        },
        "status": {
            "privacyStatus": cfg.get("publish.platforms.youtube.privacy", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video["path"], chunksize=4 * 1024 * 1024, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    youtube {int(status.progress() * 100)}%")

    vid = response["id"]
    return vid, f"https://youtube.com/shorts/{vid}"
