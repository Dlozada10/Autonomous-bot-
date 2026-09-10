"""Pull performance data back out of YouTube and into the same database.

This is what turns format rotation from a guess into a measurement. Once a
few weeks of snapshots exist, `report` ranks formats and voices by retention,
and you can retire the ones that do not work.

Two caveats worth knowing before you read the numbers:

- YouTube Analytics lags roughly 48 hours, so today's uploads show nothing.
- Shorts report retention differently from long-form, and some metrics are
  simply absent for some videos. Columns are mapped by name from the API
  response rather than by position, so a missing metric lands as NULL instead
  of silently shifting every other value one column to the left.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .publish.youtube import ANALYTICS_SCOPE, credentials

METRICS = [
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
]

# The video== filter accepts a bounded list; stay well under it.
CHUNK = 150


def _analytics(cfg: Any):
    from googleapiclient.discovery import build

    return build("youtubeAnalytics", "v2",
                 credentials=credentials(cfg, ANALYTICS_SCOPE),
                 cache_discovery=False)


def _query(service, video_ids: list[str], start: str, end: str) -> list[dict]:
    """One Analytics call. Returns a list of {video: id, metric: value}."""
    response = service.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics=",".join(METRICS),
        dimensions="video",
        filters="video==" + ",".join(video_ids),
        maxResults=len(video_ids),
    ).execute()

    # Map by column name - never assume the API returns them in request order.
    headers = [h["name"] for h in response.get("columnHeaders", [])]
    out = []
    for row in response.get("rows", []) or []:
        out.append(dict(zip(headers, row)))
    return out


def pull(cfg: Any, store: Any, days: int = 90) -> int:
    """Fetch stats for every published video. Returns how many were updated."""
    published = store.remote_ids("youtube")
    if not published:
        print("nothing published to YouTube yet")
        return 0

    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    day = end.isoformat()

    service = _analytics(cfg)
    ids = list(published)
    updated = 0

    for i in range(0, len(ids), CHUNK):
        batch = ids[i:i + CHUNK]
        try:
            rows = _query(service, batch, start.isoformat(), end.isoformat())
        except Exception as exc:
            print(f"  ! analytics query failed for {len(batch)} videos: {exc}")
            continue

        for row in rows:
            remote_id = row.get("video")
            video_id = published.get(remote_id)
            if video_id is None:
                continue
            store.record_metrics(video_id, "youtube", day, row)
            updated += 1

    have, total = store.metrics_coverage("youtube")
    print(f"updated {updated} videos ({have}/{total} published videos have data)")
    if have < total:
        print("  videos with no data are usually too recent - analytics lags ~48h")
    return updated


def report(cfg: Any, store: Any, min_videos: int = 2) -> None:
    """Print what the data says about formats and voices."""
    have, total = store.metrics_coverage("youtube")
    if not have:
        print("no metrics yet - run `python run.py metrics` first")
        return

    print(f"\nbased on {have} of {total} published videos\n")

    voice_labels = {v["id"]: v["label"] for v in (cfg.get("voice.pool") or [])}
    format_labels = {f["id"]: f["label"] for f in (cfg.get("formats.variants") or [])}

    for column, labels, heading in (
        ("format_id", format_labels, "BY FORMAT"),
        ("voice_id", voice_labels, "BY VOICE"),
    ):
        rows = store.performance_by(column, "youtube", min_videos)
        if not rows:
            print(f"{heading}\n  not enough videos per bucket yet "
                  f"(need {min_videos}+)\n")
            continue

        print(heading)
        print(f"  {'':<30} {'n':>3} {'views':>8} {'retention':>10} {'likes':>7} {'subs':>6}")
        for r in rows:
            name = labels.get(r["bucket"], r["bucket"] or "unknown")[:30]
            retention = f"{r['avg_retention']:.1f}%" if r["avg_retention"] is not None else "-"
            print(f"  {name:<30} {r['videos']:>3} {r['avg_views'] or 0:>8.0f} "
                  f"{retention:>10} {r['avg_likes'] or 0:>7.1f} {r['subs'] or 0:>6}")
        print()

    top = store.top_videos("youtube", 5)
    if top:
        print("TOP VIDEOS")
        for r in top:
            retention = f"{r['avg_view_pct']:.0f}%" if r["avg_view_pct"] is not None else "  -"
            print(f"  {r['views'] or 0:>7} views  {retention:>4} ret  "
                  f"[{r['format_id']}]  {r['title'][:52]}")
        print()

    print("Retention is the number to act on. Views follow distribution;")
    print("retention tells you whether the format itself is working.")
