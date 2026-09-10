#!/usr/bin/env python3
"""Faceless channel pipeline.

  python run.py doctor              check the environment and credentials
  python run.py discover            refresh the topic pool
  python run.py make                produce one video (no upload)
  python run.py publish             upload anything rendered but unsent
  python run.py metrics             pull YouTube performance data into the db
  python run.py report              what the data says about formats and voices
  python run.py once                make + publish, one cycle
  python run.py daemon              run forever on the configured schedule
  python run.py auth youtube        one-time YouTube OAuth consent
  python run.py demo                render a sample video with no API keys
  python run.py status              what the channel has made and posted
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from core import pipeline, topics
from core.config import Config
from core.state import Store


def _open() -> tuple[Config, Store]:
    cfg = Config()
    return cfg, Store(cfg.db_path)


# --------------------------------------------------------------------
def cmd_doctor() -> int:
    cfg, store = _open()
    ok = True

    from core.ffmpeg import FFMPEG
    have_ffmpeg = Path(FFMPEG).exists() or shutil.which(FFMPEG)
    print(f"[{'ok' if have_ffmpeg else 'XX'}] ffmpeg: {FFMPEG}")
    ok &= bool(have_ffmpeg)

    required = {"ANTHROPIC_API_KEY": "script generation"}
    optional = {
        "ELEVENLABS_API_KEY": "voice with word-level caption timing",
        "OPENAI_API_KEY": "fallback voice",
        "PEXELS_API_KEY": "stock b-roll (generated backgrounds used otherwise)",
    }
    for name, why in required.items():
        present = bool(cfg.env(name))
        print(f"[{'ok' if present else 'XX'}] {name} - {why}")
        ok &= present
    for name, why in optional.items():
        print(f"[{'ok' if cfg.env(name) else '--'}] {name} - {why}")

    for platform, settings in (cfg.get("publish.platforms", {}) or {}).items():
        if not settings.get("enabled"):
            print(f"[--] {platform}: disabled in config.yaml")
            continue
        if platform == "youtube":
            token = Path(cfg.env("YOUTUBE_TOKEN", "secrets/youtube_token.json"))
            good = token.exists()
            print(f"[{'ok' if good else 'XX'}] youtube token: {token}"
                  f"{'' if good else '  (run: python run.py auth youtube)'}")
            ok &= good
            if good:
                # A token granted before analytics was added still uploads
                # fine but cannot read performance data.
                from core.publish.youtube import ANALYTICS_SCOPE
                import json as _json
                granted = set(_json.loads(token.read_text()).get("scopes") or [])
                has_analytics = ANALYTICS_SCOPE in granted
                print(f"[{'ok' if has_analytics else '--'}] youtube analytics scope"
                      f"{'' if has_analytics else '  (re-run auth to enable `metrics`)'}")
        elif platform == "tiktok":
            good = bool(cfg.env("TIKTOK_ACCESS_TOKEN"))
            print(f"[{'ok' if good else 'XX'}] tiktok access token")
            ok &= good
        elif platform == "instagram":
            good = all(cfg.env(k) for k in
                       ("IG_USER_ID", "IG_ACCESS_TOKEN", "PUBLIC_MEDIA_BASE_URL"))
            print(f"[{'ok' if good else 'XX'}] instagram credentials + public media URL")
            ok &= good

    voices = len(cfg.get("voice.pool", []) or [])
    formats = len(cfg.get("formats.variants", []) or [])
    print(f"[ok] {formats} formats x {voices} voices in rotation")
    if cfg.get("publish.dry_run"):
        print("[!!] dry_run is ON - nothing will actually publish")

    store.close()
    print("\nready" if ok else "\nnot ready - fix the XX lines above")
    return 0 if ok else 1


def cmd_discover() -> int:
    cfg, store = _open()
    topics.refresh(cfg, store)
    store.close()
    return 0


def cmd_make() -> int:
    cfg, store = _open()
    result = pipeline.make_video(cfg, store)
    store.close()
    return 0 if result else 1


def cmd_publish() -> int:
    cfg, store = _open()
    pipeline.publish_pending(cfg, store)
    store.close()
    return 0


def cmd_once() -> int:
    cfg, store = _open()
    pipeline.cycle(cfg, store)
    store.close()
    return 0


def cmd_metrics() -> int:
    from core import metrics
    cfg, store = _open()
    try:
        metrics.pull(cfg, store)
    except Exception as exc:
        print(f"! {exc}")
        store.close()
        return 1
    store.close()
    return 0


def cmd_report() -> int:
    from core import metrics
    cfg, store = _open()
    metrics.report(cfg, store)
    store.close()
    return 0


def cmd_status() -> int:
    cfg, store = _open()
    rows = store.conn.execute(
        "SELECT v.slug, v.format_id, v.score, v.duration, v.created_at,"
        " (SELECT GROUP_CONCAT(platform) FROM uploads u"
        "  WHERE u.video_id = v.id AND u.status='ok') AS posted"
        " FROM videos v ORDER BY v.created_at DESC LIMIT 15"
    ).fetchall()
    if not rows:
        print("nothing made yet")
    for r in rows:
        when = dt.datetime.fromtimestamp(r["created_at"]).strftime("%m-%d %H:%M")
        print(f"{when}  {r['format_id']:<10} {r['duration'] or 0:>5.1f}s "
              f"score {r['score'] or 0:>3.0f}  [{r['posted'] or 'unposted'}]  {r['slug']}")
    for platform in (cfg.get("publish.platforms", {}) or {}):
        print(f"{platform}: {store.uploads_today(platform)} posted in last 24h")
    store.close()
    return 0


def cmd_daemon() -> int:
    cfg, store = _open()
    tz = ZoneInfo(cfg.get("publish.schedule.timezone", "UTC"))
    slots = cfg.get("publish.schedule.slots", ["09:00"])
    print(f"daemon up. slots {slots} ({tz})  ctrl-c to stop")

    while True:
        now = dt.datetime.now(tz)
        upcoming = []
        for slot in slots:
            hour, minute = (int(x) for x in slot.split(":"))
            when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when <= now:
                when += dt.timedelta(days=1)
            upcoming.append(when)
        target = min(upcoming)

        wait = (target - now).total_seconds()
        print(f"next run {target:%Y-%m-%d %H:%M %Z} (in {wait / 3600:.1f}h)")
        time.sleep(wait)

        try:
            pipeline.cycle(cfg, store)
        except Exception as exc:
            # A daemon that dies at 3am is worse than one that skips a slot.
            print(f"! cycle failed: {exc}")
        time.sleep(60)   # never double-fire a slot


def cmd_auth(target: str) -> int:
    cfg, store = _open()
    if target == "youtube":
        from core.publish.youtube import authorize
        authorize(cfg)
    else:
        print(f"unknown auth target: {target}")
        return 1
    store.close()
    return 0


def cmd_demo() -> int:
    """Render a sample with no API keys, to prove the video path works."""
    from core.script import Beat, Script
    from core.render import render
    from core import voice as voice_mod

    cfg, store = _open()
    cfg.data["voice"]["provider"] = "silent"

    script = Script(
        title="The agent framework nobody needed",
        hook=Beat(voiceover="Most AI agent frameworks solve a problem you do not have.",
                  caption="You Don't Need It", b_roll="server racks"),
        beats=[
            Beat(voiceover="They wrap a single API call in four layers of abstraction.",
                 caption="Four Layers", b_roll="tangled cables"),
            Beat(voiceover="Then you spend a week learning the wrapper instead of the model.",
                 caption="A Week Gone", b_roll="person at laptop"),
            Beat(voiceover="Start with a plain loop and add structure only when it hurts.",
                 caption="Start Plain", b_roll="clean desk"),
        ],
        payoff=Beat(voiceover="The best framework is usually the one you did not install.",
                    caption="Install Nothing", b_roll="empty terminal"),
        description="Why most agent frameworks are premature abstraction.",
        hashtags=["ai", "automation", "devtools", "agents"],
    )
    fmt = {"id": "mistake", "label": "The thing people get wrong",
           "beats": 3, "visual": "warn_pulse"}

    work = cfg.out_dir / "work" / "demo"
    work.mkdir(parents=True, exist_ok=True)
    clips = [voice_mod.synthesize(b.voiceover, "", work / f"b{i}.mp3", cfg)
             for i, b in enumerate(script.lines())]
    path, duration = render(script, clips, fmt, cfg.out_dir / "demo.mp4", cfg, work)
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {path} ({duration:.1f}s)")
    store.close()
    return 0


COMMANDS = {
    "doctor": cmd_doctor, "discover": cmd_discover, "make": cmd_make,
    "publish": cmd_publish, "once": cmd_once, "daemon": cmd_daemon,
    "status": cmd_status, "demo": cmd_demo, "metrics": cmd_metrics,
    "report": cmd_report,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    name = sys.argv[1]
    if name == "auth":
        return cmd_auth(sys.argv[2] if len(sys.argv) > 2 else "")
    fn = COMMANDS.get(name)
    if not fn:
        print(f"unknown command: {name}\n{__doc__}")
        return 1
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
