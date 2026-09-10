#!/usr/bin/env python3
"""Mutation testing for the logic that fails silently.

A passing test suite only proves the tests agree with the code, not that they
would notice if the code were wrong. This deliberately breaks dedupe and
cooldown one line at a time and checks the suite turns red.

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

    print(f"all {len(MUTANTS)} mutants killed - dedupe and cooldown are covered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
