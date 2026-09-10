"""Assemble the finished vertical video with ffmpeg.

Backgrounds are generated procedurally by default. That is deliberate: it
removes any dependency on stock-footage APIs, rate limits, licences, or a
local asset library, so an unattended run can never fail for want of a clip.
Pexels b-roll is used instead when a key is present.
"""
from __future__ import annotations

import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .captions import THEMES, Cue, build as build_captions
from .ffmpeg import FFMPEG
from .voice import Clip, Word

BEAT_GAP = 0.20   # breathing room between beats, seconds


class RenderError(RuntimeError):
    pass


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RenderError(f"ffmpeg failed:\n{tail}")


# --------------------------------------------------------------------
#  Audio
# --------------------------------------------------------------------
def build_audio(clips: list[Clip], out: Path) -> tuple[Path, list[Cue], float]:
    """Concatenate beat audio with gaps; return absolute word timings."""
    args = [FFMPEG, "-y"]
    for clip in clips:
        args += ["-i", str(clip.path)]

    chains, labels = [], []
    for i in range(len(clips)):
        chains.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono,"
                      f"apad=pad_dur={BEAT_GAP}[a{i}]")
        labels.append(f"[a{i}]")
    chains.append(f"{''.join(labels)}concat=n={len(clips)}:v=0:a=1[out]")

    args += ["-filter_complex", ";".join(chains), "-map", "[out]",
             "-c:a", "pcm_s16le", str(out)]
    _run(args)

    # Shift each beat's word timings onto the video timeline.
    cues: list[Cue] = []
    cursor = 0.0
    for clip in clips:
        shifted = [Word(w.text, w.start + cursor, w.end + cursor) for w in clip.words]
        cues.append(Cue(label="", words=shifted))
        cursor += clip.duration + BEAT_GAP
    return out, cues, cursor


def add_music(voice: Path, out: Path, assets_dir: Path) -> Path:
    """Mix in a music bed if one exists. Silently a no-op otherwise."""
    music_dir = assets_dir / "music"
    tracks = sorted(music_dir.glob("*.mp3")) + sorted(music_dir.glob("*.m4a")) \
        if music_dir.exists() else []
    if not tracks:
        return voice

    track = random.choice(tracks)
    _run([
        FFMPEG, "-y", "-i", str(voice), "-stream_loop", "-1", "-i", str(track),
        "-filter_complex",
        # Duck the bed well under the voice, and fade it out at the end.
        "[1:a]volume=0.07,afade=t=out:st=0:d=0[bed];"
        "[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0[out]",
        "-map", "[out]", "-c:a", "pcm_s16le", str(out),
    ])
    return out


# --------------------------------------------------------------------
#  Background
# --------------------------------------------------------------------
def _gradient_source(theme: dict, duration: float, w: int, h: int, fps: int) -> str:
    """An animated two-tone gradient. Cheap, licence-free, always available."""
    a = theme["bg_a"].lstrip("#")
    b = theme["bg_b"].lstrip("#")
    seed = random.randint(0, 100000)
    return (
        f"gradients=s={w}x{h}:c0=0x{a}:c1=0x{b}:x0={random.randint(0, w)}:"
        f"y0={random.randint(0, h)}:x1={random.randint(0, w)}:y1={random.randint(0, h)}:"
        f"speed=0.012:seed={seed}:d={duration:.2f}:r={fps}"
    )


def fetch_broll(queries: list[str], work: Path, cfg: Any) -> list[Path]:
    """Download one vertical clip per beat from Pexels. Best-effort."""
    key = cfg.env("PEXELS_API_KEY")
    if not key:
        return []
    out: list[Path] = []
    for i, query in enumerate(queries):
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": key},
                params={"query": query, "orientation": "portrait",
                        "size": "medium", "per_page": 5},
                timeout=30,
            )
            resp.raise_for_status()
            videos = resp.json().get("videos") or []
            if not videos:
                continue
            files = sorted(
                random.choice(videos)["video_files"],
                key=lambda f: abs((f.get("height") or 0) - 1920),
            )
            dest = work / f"broll_{i}.mp4"
            with requests.get(files[0]["link"], stream=True, timeout=90) as stream:
                stream.raise_for_status()
                with dest.open("wb") as fh:
                    shutil.copyfileobj(stream.raw, fh)
            out.append(dest)
        except Exception as exc:
            print(f"  ! b-roll '{query}' failed: {exc}")
    return out


def _broll_chain(clips: list[Path], spans: list[float], w: int, h: int,
                 fps: int) -> tuple[list[str], str]:
    """Scale/crop each clip to fill the vertical frame, then concat."""
    chains, labels = [], []
    for i, span in enumerate(spans):
        chains.append(
            f"[{i}:v]trim=0:{span:.2f},setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},fps={fps},setsar=1,"
            # Slow push-in keeps a short static clip from reading as a freeze.
            f"zoompan=z='min(zoom+0.0004,1.12)':d=1:s={w}x{h}:fps={fps}[v{i}]"
        )
        labels.append(f"[v{i}]")
    chains.append(f"{''.join(labels)}concat=n={len(spans)}:v=1:a=0[bg]")
    return chains, "[bg]"


# --------------------------------------------------------------------
#  Assembly
# --------------------------------------------------------------------
def render(script: Any, clips: list[Clip], fmt: dict, out_path: Path,
           cfg: Any, work: Path) -> tuple[Path, float]:
    """Produce the final MP4. Returns (path, duration)."""
    work.mkdir(parents=True, exist_ok=True)
    w = int(cfg.get("video.width", 1080))
    h = int(cfg.get("video.height", 1920))
    fps = int(cfg.get("video.fps", 30))
    theme = THEMES.get(fmt.get("visual", ""), THEMES["gradient_drift"])

    # 1. Voice track and the timeline it implies.
    voice_wav, cues, duration = build_audio(clips, work / "voice.wav")

    max_seconds = float(cfg.get("video.max_seconds", 58))
    if duration > max_seconds:
        raise RenderError(
            f"narration runs {duration:.1f}s, over the {max_seconds:.0f}s ceiling"
        )

    # 2. Attach each beat's on-screen label to its cue.
    for cue, beat in zip(cues, script.lines()):
        cue.label = beat.caption

    ass = build_captions(
        cues, work / "captions.ass", w, h,
        font=cfg.get("video.font", "DejaVu Sans"), theme=theme,
    )

    audio = add_music(voice_wav, work / "mixed.wav", cfg.assets_dir)

    # 3. Background: b-roll when available, generated gradient otherwise.
    spans = [c.duration + BEAT_GAP for c in clips]
    broll = fetch_broll([b.b_roll for b in script.lines()], work, cfg)

    args = [FFMPEG, "-y"]
    if len(broll) == len(spans):
        for clip in broll:
            args += ["-stream_loop", "-1", "-i", str(clip)]
        chains, bg_label = _broll_chain(broll, spans, w, h, fps)
        audio_index = len(broll)
    else:
        if broll:
            print("  ! partial b-roll, using generated background instead")
        args += ["-f", "lavfi", "-i", _gradient_source(theme, duration, w, h, fps)]
        chains, bg_label = [], "[0:v]"
        audio_index = 1

    args += ["-i", str(audio)]

    # 4. Grade the background, then burn in the captions.
    escaped = str(ass).replace("\\", "/").replace(":", r"\:")
    chains.append(
        f"{bg_label}format=yuv420p,eq=saturation=1.15:contrast=1.05,"
        f"vignette=PI/4.5,noise=alls=6:allf=t+u,"
        f"subtitles='{escaped}':fontsdir=/usr/share/fonts[vout]"
    )

    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[vout]", "-map", f"{audio_index}:a",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args)
    return out_path, duration
