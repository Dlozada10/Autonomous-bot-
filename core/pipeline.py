"""Orchestration: topic -> script -> voice -> render -> publish."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import anthropic

from . import script as script_mod
from . import topics, voice
from .publish import PUBLISHERS
from .render import RenderError, render


# One run must not be able to drain the whole pool. If three topics in a row
# fail, something systemic is wrong and burning 25 more will not fix it.
MAX_TOPICS_PER_RUN = 3


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{base or 'video'}"


def make_video(cfg: Any, store: Any) -> int | None:
    """Produce exactly one video. Returns its row id, or None if nothing shipped."""
    client = anthropic.Anthropic(api_key=cfg.require("ANTHROPIC_API_KEY"))

    pool = store.unused_topics(25)
    if not pool:
        topics.refresh(cfg, store)
        pool = store.unused_topics(25)
    if not pool:
        print("no topics available")
        return None

    for row in pool[:MAX_TOPICS_PER_RUN]:
        topic = dict(row)
        print(f"\ntopic: {topic['title'][:76]}")
        # Count the attempt rather than consuming the topic. A topic is only
        # retired once it has produced a video, or failed enough times to look
        # genuinely unusable - an API outage should not burn the whole queue.
        store.note_attempt(topic["id"])

        # Most feeds carry a headline and little else. Fetch the article so the
        # writer has something specific to work from; without this it correctly
        # refuses every topic as too thin.
        topic["summary"] = topics.enrich(topic)
        print(f"  source: {len(topic['summary'])} chars")
        if len(topic["summary"]) < topics.MIN_SOURCE_CHARS:
            print("  ! not enough source material, skipping")
            continue

        try:
            result = script_mod.produce(client, cfg, store, topic)
        except Exception as exc:
            # Previously this propagated and killed the run with a traceback,
            # or was lost entirely. Say what went wrong and move on.
            print(f"  ! generation failed: {type(exc).__name__}: {exc}")
            continue
        if result is None:
            continue
        script, fmt, score = result

        chosen = voice.pick_voice(cfg, store)
        print(f"  voice: {chosen['label']}")

        slug = _slug(script.title)
        work = cfg.out_dir / "work" / slug
        work.mkdir(parents=True, exist_ok=True)
        try:
            clips = [
                voice.synthesize(beat.voiceover, chosen["id"], work / f"beat_{i}.mp3", cfg)
                for i, beat in enumerate(script.lines())
            ]
            out_path = cfg.out_dir / f"{slug}.mp4"
            path, duration = render(script, clips, fmt, out_path, cfg, work)
        except RenderError as exc:
            print(f"  ! render failed: {exc}")
            continue
        finally:
            shutil.rmtree(work, ignore_errors=True)

        store.mark_topic_used(topic["id"])
        video_id = store.add_video(
            slug=slug, topic_id=topic["id"], format_id=fmt["id"],
            voice_id=chosen["id"], title=script.title,
            script=json.loads(script.model_dump_json()),
            score=score, duration=duration, path=str(path),
        )
        print(f"  rendered {path.name} ({duration:.1f}s, score {score:.0f})")
        return video_id

    print(f"no topic produced a usable video this run "
          f"(tried {min(len(pool), MAX_TOPICS_PER_RUN)}). "
          f"Run `python run.py check` if this keeps happening.")
    return None


def publish_pending(cfg: Any, store: Any) -> None:
    """Push rendered videos to every enabled platform, respecting daily caps."""
    if cfg.get("publish.dry_run"):
        print("dry_run is on - holding everything for review")
        return

    for platform, settings in (cfg.get("publish.platforms", {}) or {}).items():
        if not settings.get("enabled"):
            continue
        cap = int(settings.get("max_per_day", 3))
        sent = store.uploads_today(platform)
        if sent >= cap:
            print(f"{platform}: daily cap reached ({sent}/{cap})")
            continue

        for row in store.pending_uploads(platform):
            if sent >= cap:
                break
            video = dict(row)
            if not Path(video["path"]).exists():
                store.add_upload(video["id"], platform, "error", detail="file missing")
                continue

            print(f"{platform}: uploading {video['slug']}")
            try:
                remote_id, url = PUBLISHERS[platform](
                    video, json.loads(video["script"]), cfg
                )
                store.add_upload(video["id"], platform, "ok", remote_id, url)
                sent += 1
                print(f"  -> {url}")
            except Exception as exc:
                # Recorded, not raised: one platform failing must not stop
                # the others, and the video stays queued for the next run.
                store.add_upload(video["id"], platform, "error", detail=str(exc)[:500])
                print(f"  ! {platform} failed: {exc}")


def refresh_metrics(cfg: Any, store: Any) -> None:
    """Best-effort analytics pull. Never allowed to block making a video."""
    try:
        from . import metrics
        metrics.pull(cfg, store)
    except Exception as exc:
        print(f"! metrics pull skipped: {exc}")


def cycle(cfg: Any, store: Any) -> None:
    """One full turn of the crank: make a video, publish, then update stats."""
    make_video(cfg, store)
    publish_pending(cfg, store)
    refresh_metrics(cfg, store)
