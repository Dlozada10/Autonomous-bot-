"""Text to speech with word-level timing.

Timing is the whole point. Karaoke-style captions - the word highlighting on
every successful short-form channel - need to know when each word is spoken.
ElevenLabs returns character-level alignment, which we fold into word spans.
Other providers get an estimate good enough to look deliberate.
"""
from __future__ import annotations

import base64
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .ffmpeg import FFMPEG

TIMEOUT = 120


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Clip:
    """One spoken beat: an audio file plus when each word lands inside it."""
    path: Path
    duration: float
    words: list[Word]


# --------------------------------------------------------------------
#  Timing helpers
# --------------------------------------------------------------------
def _weight(word: str) -> float:
    """Rough spoken length. Vowel groups approximate syllables well enough."""
    letters = [c for c in word.lower() if c.isalpha()]
    if not letters:
        return 0.5
    groups, prev_vowel = 0, False
    for ch in letters:
        is_vowel = ch in "aeiouy"
        if is_vowel and not prev_vowel:
            groups += 1
        prev_vowel = is_vowel
    return max(1.0, float(groups))


def estimate_words(text: str, duration: float) -> list[Word]:
    """Distribute a known clip duration across words by syllable weight."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [_weight(t) for t in tokens]
    total = sum(weights) or 1.0
    words, cursor = [], 0.0
    for token, w in zip(tokens, weights):
        span = duration * (w / total)
        words.append(Word(token, cursor, cursor + span))
        cursor += span
    return words


def _chars_to_words(text: str, chars: list[str], starts: list[float],
                    ends: list[float]) -> list[Word]:
    """Fold ElevenLabs character alignment into word spans."""
    words: list[Word] = []
    buf, w_start, w_end = "", None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append(Word(buf, w_start, w_end))
                buf, w_start, w_end = "", None, None
            continue
        if not buf:
            w_start = s
        buf += ch
        w_end = e
    if buf:
        words.append(Word(buf, w_start, w_end))
    return words or estimate_words(text, ends[-1] if ends else 1.0)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [FFMPEG, "-i", str(path), "-hide_banner"],
        capture_output=True, text=True,
    ).stderr
    for line in out.splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = stamp.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


# --------------------------------------------------------------------
#  Providers
# --------------------------------------------------------------------
def _elevenlabs(text: str, voice_id: str, out: Path, cfg: Any, api_key: str) -> Clip:
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": cfg.get("voice.model", "eleven_turbo_v2_5"),
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
                "speed": float(cfg.get("voice.speed", 1.0)),
            },
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    out.write_bytes(base64.b64decode(payload["audio_base64"]))

    align = payload.get("normalized_alignment") or payload.get("alignment") or {}
    chars = align.get("characters") or []
    if chars:
        words = _chars_to_words(
            text, chars,
            align["character_start_times_seconds"],
            align["character_end_times_seconds"],
        )
    else:
        words = estimate_words(text, probe_duration(out))
    return Clip(out, probe_duration(out), words)


def _openai(text: str, voice_id: str, out: Path, cfg: Any, api_key: str) -> Clip:
    resp = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "tts-1", "voice": voice_id or "onyx", "input": text,
              "speed": float(cfg.get("voice.speed", 1.0))},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out.write_bytes(resp.content)
    duration = probe_duration(out)
    return Clip(out, duration, estimate_words(text, duration))


def _silent(text: str, out: Path, cfg: Any) -> Clip:
    """No API key needed. Renders a real video with the right pacing so the
    visual pipeline can be developed and tested offline."""
    words = text.split()
    duration = max(1.0, len(words) / (2.6 * float(cfg.get("voice.speed", 1.0))))
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i",
         f"anullsrc=channel_layout=mono:sample_rate=44100:d={duration:.3f}",
         "-c:a", "libmp3lame", "-q:a", "6", str(out)],
        check=True, capture_output=True,
    )
    return Clip(out, duration, estimate_words(text, duration))


# --------------------------------------------------------------------
#  Entry points
# --------------------------------------------------------------------
def pick_voice(cfg: Any, store: Any) -> dict:
    """Rotate voices so consecutive videos don't sound identical."""
    pool = cfg.get("voice.pool", []) or [{"id": "", "label": "default"}]
    cooldown = int(cfg.get("voice.cooldown", 2))
    recent = set(store.recent_values("voice_id", cooldown))
    eligible = [v for v in pool if v["id"] not in recent] or pool
    return random.choice(eligible)


def synthesize(text: str, voice_id: str, out: Path, cfg: Any) -> Clip:
    """Speak one beat. Falls back down the provider chain on missing keys."""
    provider = cfg.get("voice.provider", "elevenlabs")
    out.parent.mkdir(parents=True, exist_ok=True)

    if provider == "elevenlabs":
        key = cfg.env("ELEVENLABS_API_KEY")
        if key:
            return _elevenlabs(text, voice_id, out, cfg, key)
        print("  ! ELEVENLABS_API_KEY unset, trying OpenAI")
        provider = "openai"

    if provider == "openai":
        key = cfg.env("OPENAI_API_KEY")
        if key:
            return _openai(text, voice_id, out, cfg, key)
        print("  ! OPENAI_API_KEY unset, falling back to silent audio")

    return _silent(text, out, cfg)
