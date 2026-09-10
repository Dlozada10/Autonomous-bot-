#!/usr/bin/env python3
"""Mutation testing for the logic that fails silently.

Covers dedupe, cooldown rotation, the quality gate, and the render path.

A passing test suite only proves the tests agree with the code, not that they
would notice if the code were wrong. This deliberately breaks that
logic one line at a time and checks the suite turns red.

A SURVIVED mutant is a hole: the code was broken and nothing complained.

    python tests/mutate.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (area, description, file, find, replace)
MUTANTS = [
    # ---- dedupe: normalisation ----
    ("dedupe", "token order stops being ignored", "core/state.py",
     'return " ".join(sorted(tokens))',
     'return " ".join(tokens)'),
    ("dedupe", "stopwords are no longer stripped", "core/state.py",
     'tokens = [t for t in cleaned.split() if t and t not in _STOPWORDS]',
     'tokens = [t for t in cleaned.split() if t]'),
    ("dedupe", "case stops being folded", "core/state.py",
     'cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in text)',
     'cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)'),

    # ---- dedupe: the comparison itself ----
    ("dedupe", "similarity check disabled entirely", "core/state.py",
     '            if SequenceMatcher(None, fp, prior).ratio() >= threshold:\n                return True',
     '            if False:\n                return True'),
    ("dedupe", "threshold boundary flipped (>= becomes >)", "core/state.py",
     'if SequenceMatcher(None, fp, prior).ratio() >= threshold:',
     'if SequenceMatcher(None, fp, prior).ratio() > threshold:'),
    ("dedupe", "unpublished topics also block new ones", "core/state.py",
     '"SELECT fingerprint FROM topics WHERE used_at IS NOT NULL AND used_at > ?",',
     '"SELECT fingerprint FROM topics WHERE used_at > ? OR 1=1",'),
    ("dedupe", "dedupe window never expires", "core/state.py",
     '"SELECT fingerprint FROM topics WHERE used_at IS NOT NULL AND used_at > ?",',
     '"SELECT fingerprint FROM topics WHERE used_at IS NOT NULL AND used_at > ? - 1e12",'),

    # ---- cooldown: history lookup ----
    ("cooldown", "recent history returned oldest-first", "core/state.py",
     'f"SELECT {column} FROM videos ORDER BY created_at DESC LIMIT ?", (limit,)',
     'f"SELECT {column} FROM videos ORDER BY created_at ASC LIMIT ?", (limit,)'),
    ("cooldown", "cooldown window size ignored", "core/state.py",
     'f"SELECT {column} FROM videos ORDER BY created_at DESC LIMIT ?", (limit,)',
     'f"SELECT {column} FROM videos ORDER BY created_at DESC LIMIT -1", ()'),
    ("cooldown", "column allow-list removed", "core/state.py",
     '        if column not in {"format_id", "voice_id"}:\n            raise ValueError(f"not a rotatable column: {column}")',
     '        pass'),

    # ---- cooldown: selection ----
    ("cooldown", "format cooldown not applied", "core/script.py",
     'eligible = [v for v in variants if v["id"] not in recent] or variants',
     'eligible = variants'),
    ("cooldown", "format fallback removed when all are cooling", "core/script.py",
     'eligible = [v for v in variants if v["id"] not in recent] or variants',
     'eligible = [v for v in variants if v["id"] not in recent]'),
    ("cooldown", "voice cooldown not applied", "core/voice.py",
     'eligible = [v for v in pool if v["id"] not in recent] or pool',
     'eligible = pool'),
    ("cooldown", "voice fallback removed for an empty pool", "core/voice.py",
     'pool = cfg.get("voice.pool", []) or [{"id": "", "label": "default"}]',
     'pool = cfg.get("voice.pool", [])'),

    # ---- quality gate: the floors ----
    ("quality", "floors not enforced at all", "core/script.py",
     '        if value < int(floor):\n            return False, f"{dim}={value} below floor {floor}: {g.verdict}"',
     '        if False:\n            return False, f"{dim}={value} below floor {floor}: {g.verdict}"'),
    ("quality", "floor boundary flipped (< becomes <=)", "core/script.py",
     'if value < int(floor):',
     'if value <= int(floor):'),
    ("quality", "typo'd floor key silently ignored", "core/script.py",
     "    if unknown:\n        raise ValueError(",
     "    if False:\n        raise ValueError("),

    # ---- quality gate: the aggregate ----
    ("quality", "average threshold not enforced", "core/script.py",
     '    if total < minimum:',
     '    if False:'),
    ("quality", "average boundary flipped (< becomes <=)", "core/script.py",
     '    if total < minimum:',
     '    if total <= minimum:'),
    ("quality", "best dimension used instead of the mean", "core/script.py",
     'total = sum(dims) / len(dims)',
     'total = max(dims)'),
    ("quality", "gate always passes", "core/script.py",
     'def passes(cfg: Any, g: Grade) -> tuple[bool, str]:\n    """Apply the configured floors and the aggregate minimum."""',
     'def passes(cfg: Any, g: Grade) -> tuple[bool, str]:\n    """Apply the configured floors and the aggregate minimum."""\n    return True, "mutant"'),

    # ---- render: the Shorts ceiling ----
    ("render", "length ceiling not enforced", "core/render.py",
     '    if duration > max_seconds:',
     '    if False:'),
    ("render", "ceiling boundary flipped (> becomes >=)", "core/render.py",
     '    if duration > max_seconds:',
     '    if duration >= max_seconds:'),

    # ---- render: the video itself ----
    ("render", "captions never burned in", "core/render.py",
     "f\"subtitles='{escaped}':fontsdir=/usr/share/fonts[vout]\"",
     "f\"null[vout]\""),
    ("render", "frame rendered landscape instead of vertical", "core/render.py",
     '    w = int(cfg.get("video.width", 1080))\n    h = int(cfg.get("video.height", 1920))',
     '    h = int(cfg.get("video.width", 1080))\n    w = int(cfg.get("video.height", 1920))'),

    # ---- render: the audio timeline ----
    ("render", "beat gap removed from the timeline", "core/render.py",
     'BEAT_GAP = 0.20',
     'BEAT_GAP = 0.0'),
    ("render", "word timings not shifted onto the timeline", "core/render.py",
     'shifted = [Word(w.text, w.start + cursor, w.end + cursor) for w in clip.words]',
     'shifted = [Word(w.text, w.start, w.end) for w in clip.words]'),
    ("render", "cursor advances without the gap", "core/render.py",
     'cursor += clip.duration + BEAT_GAP',
     'cursor += clip.duration'),
]


def run_suite() -> bool:
    """True if the suite passes."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.returncode == 0


def main() -> int:
    if not run_suite():
        print("baseline suite is already failing - fix that first")
        return 2
    print(f"baseline green. applying {len(MUTANTS)} mutants\n")

    survived = []
    for area, description, rel, find, replace in MUTANTS:
        target = ROOT / rel
        original = target.read_text()
        if find not in original:
            print(f"  SKIP   [{area}] {description}  (source moved - update the mutant)")
            survived.append((area, description, "stale"))
            continue

        backup = Path(tempfile.mkdtemp()) / target.name
        shutil.copy(target, backup)
        try:
            target.write_text(original.replace(find, replace, 1))
            killed = not run_suite()
        finally:
            shutil.copy(backup, target)

        status = "killed" if killed else "SURVIVED"
        print(f"  {status:<8} [{area}] {description}")
        if not killed:
            survived.append((area, description, "survived"))

    print()
    if survived:
        print(f"{len(survived)} of {len(MUTANTS)} mutants were not caught:")
        for area, description, why in survived:
            print(f"  - [{area}] {description} ({why})")
        print("\nEach one is a change that breaks the channel silently.")
        return 1

    areas = sorted({area for area, *_ in MUTANTS})
    print(f"all {len(MUTANTS)} mutants killed across: {', '.join(areas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
