"""Topic discovery: pull candidates, rank them, hand back one fresh idea.

Deliberately dependency-free (stdlib XML + requests). Any source that fails
is skipped with a warning rather than taking the run down - a news feed
being briefly unreachable should never stop the day's video.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; faceless-pipeline/1.0)"}
TIMEOUT = 20


@dataclass
class Candidate:
    title: str
    url: str
    source: str
    summary: str
    published: float   # unix seconds; 0 when unknown
    weight: float = 1.0


def _strip_tags(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _parse_date(value: str) -> float:
    """RSS and Atom disagree about date formats; try both."""
    if not value:
        return 0.0
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def from_rss(url: str) -> list[Candidate]:
    resp = requests.get(url, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    out: list[Candidate] = []
    # RSS 2.0
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        out.append(Candidate(
            title=title,
            url=(item.findtext("link") or "").strip(),
            source=url,
            summary=_strip_tags(item.findtext("description") or "")[:1200],
            published=_parse_date(item.findtext("pubDate") or ""),
        ))
    # Atom
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{ns}entry"):
        title = (entry.findtext(f"{ns}title") or "").strip()
        if not title:
            continue
        link_el = entry.find(f"{ns}link")
        body = entry.findtext(f"{ns}summary") or entry.findtext(f"{ns}content") or ""
        out.append(Candidate(
            title=title,
            url=(link_el.get("href") if link_el is not None else "") or "",
            source=url,
            summary=_strip_tags(body)[:1200],
            published=_parse_date(entry.findtext(f"{ns}updated") or ""),
        ))
    return out


def from_hackernews(query: str, min_points: int) -> list[Candidate]:
    resp = requests.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={
            "query": query,
            "tags": "story",
            "numericFilters": f"points>{min_points}",
            "hitsPerPage": 40,
        },
        headers=UA, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for hit in resp.json().get("hits", []):
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        points = hit.get("points") or 0
        out.append(Candidate(
            title=title,
            url=hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
            source="hackernews",
            summary=(hit.get("story_text") or "")[:1200],
            published=float(hit.get("created_at_i") or 0),
            # Community signal is a real quality prior - let it bias ranking.
            weight=1.0 + min(points / 600.0, 0.8),
        ))
    return out


def from_github_trending(language: str) -> list[Candidate]:
    """No official trending API - approximate it with recently-created repos."""
    since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    resp = requests.get(
        "https://api.github.com/search/repositories",
        params={
            "q": f"created:>{since} stars:>250 topic:ai language:{language}",
            "sort": "stars", "order": "desc", "per_page": 20,
        },
        headers={**UA, "Accept": "application/vnd.github+json"}, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for repo in resp.json().get("items", []):
        out.append(Candidate(
            title=f"{repo['name']}: {repo.get('description') or 'new AI project'}",
            url=repo["html_url"],
            source="github",
            summary=(repo.get("description") or "")[:1200],
            published=_parse_date(repo.get("created_at", "")),
            weight=1.0 + min((repo.get("stargazers_count") or 0) / 5000.0, 0.6),
        ))
    return out


def gather(cfg: Any) -> list[Candidate]:
    """Collect from every configured source, tolerating individual failures."""
    src = cfg.get("topics.sources", {})
    found: list[Candidate] = []

    for feed in src.get("rss", []) or []:
        try:
            got = from_rss(feed)
            found.extend(got)
            print(f"  rss {feed} -> {len(got)}")
        except Exception as exc:
            print(f"  ! rss {feed} failed: {exc}")

    hn = src.get("hackernews", {}) or {}
    if hn.get("enabled"):
        try:
            got = from_hackernews(hn.get("query", "AI"), int(hn.get("min_points", 100)))
            found.extend(got)
            print(f"  hackernews -> {len(got)}")
        except Exception as exc:
            print(f"  ! hackernews failed: {exc}")

    gh = src.get("github_trending", {}) or {}
    if gh.get("enabled"):
        try:
            got = from_github_trending(gh.get("language", "python"))
            found.extend(got)
            print(f"  github -> {len(got)}")
        except Exception as exc:
            print(f"  ! github failed: {exc}")

    return found


def rank(candidates: list[Candidate], max_age_days: int) -> list[Candidate]:
    """Freshness x source weight. Recency dominates - this is a news-ish niche."""
    now = time.time()
    horizon = max_age_days * 86400
    scored: list[tuple[float, Candidate]] = []
    for c in candidates:
        if not c.title or len(c.title) < 15:
            continue
        age = now - c.published if c.published else horizon * 0.5
        if age > horizon:
            continue
        freshness = max(0.0, 1.0 - (age / horizon))
        scored.append((freshness * c.weight, c))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored]


def refresh(cfg: Any, store: Any) -> int:
    """Discover topics and persist the new ones. Returns how many were added."""
    print("discovering topics...")
    ranked = rank(gather(cfg), int(cfg.get("topics.max_age_days", 21)))
    ranked = ranked[: int(cfg.get("topics.pool_size", 60))]

    days = int(cfg.get("topics.dedupe_days", 120))
    threshold = float(cfg.get("topics.similarity_threshold", 0.62))

    added = 0
    for c in ranked:
        if store.is_too_similar(c.title, days, threshold):
            continue
        if store.add_topic(c.title, c.url, c.source, c.summary) is not None:
            added += 1
    print(f"  {added} new topics banked ({len(ranked)} ranked candidates)")
    return added
