"""Script generation and the quality gate.

Two Claude calls per video:
  1. write()  - produces a structured script for a chosen format variant
  2. grade()  - independently scores it; low scores are rejected and retried

The grader is the load-bearing part of running unattended. It is what stands
between an auto-publishing channel and the one bad video that costs it.
"""
from __future__ import annotations

import json
import random
from typing import Any

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-opus-5"


# --------------------------------------------------------------------
#  Schemas
# --------------------------------------------------------------------
class Beat(BaseModel):
    voiceover: str = Field(description="Exactly what the narrator says. One or two sentences, spoken register, no stage directions.")
    caption: str = Field(description="On-screen text for this beat. 2-5 words, no ending punctuation. Not a copy of the voiceover.")
    b_roll: str = Field(description="Two or three words describing footage for this beat, e.g. 'server racks' or 'person typing laptop'.")


class Script(BaseModel):
    format_id: str = Field(description="The id of the structure you chose from the list offered. Pick the one the source can actually support.")
    title: str = Field(description="YouTube Shorts title, under 70 characters. Specific and concrete. No clickbait punctuation, no all-caps words.")
    hook: Beat = Field(description="The first 3 seconds. Must name something specific and create a real question in the viewer's mind.")
    beats: list[Beat] = Field(description="The body of the video, in order.")
    payoff: Beat = Field(description="The closing beat. Resolves the hook with something the viewer can act on. No 'like and subscribe'.")
    description: str = Field(description="2-3 sentence description for the platform. Plain, informative.")
    hashtags: list[str] = Field(description="4-6 lowercase hashtags without the # symbol.")

    def lines(self) -> list[Beat]:
        return [self.hook, *self.beats, self.payoff]

    def spoken_text(self) -> str:
        return " ".join(b.voiceover.strip() for b in self.lines())

    def word_count(self) -> int:
        return len(self.spoken_text().split())


class Grade(BaseModel):
    hook_strength: int = Field(description="0-100. Would a scrolling viewer stop in the first 3 seconds? Generic openers score below 50.")
    factual_grounding: int = Field(description="0-100. Is every claim supported by the supplied source material? Any invented number, price, benchmark, or feature is an automatic score below 40.")
    originality: int = Field(description="0-100. Does this say something the viewer could not get from the headline alone? Recycled listicle phrasing scores below 50.")
    policy_safety: int = Field(description="0-100. Free of medical/financial/legal advice, unverifiable superlatives, engagement bait, and anything defamatory about a named company. 100 means clean.")
    verdict: str = Field(description="One sentence explaining the lowest-scoring dimension.")
    fix: str = Field(description="One concrete instruction that would raise the weakest score. Empty string if the script passes cleanly.")


# --------------------------------------------------------------------
#  Prompting
# --------------------------------------------------------------------
def _system(cfg: Any, eligible: list[dict]) -> str:
    forbidden = "\n".join(f"- {item}" for item in cfg.get("channel.forbidden", []))
    seconds = cfg.get("video.target_seconds", 38)
    # ~2.6 words/second at a natural short-form delivery pace.
    budget = int(seconds * 2.6)
    structures = _describe(eligible)
    return f"""You write scripts for {cfg.get('channel.name')}, a short-form vertical video channel about {cfg.get('channel.niche')}.

AUDIENCE
{cfg.get('channel.audience')}

CHOOSE THE STRUCTURE
Pick whichever of these the source can actually support, and return its id in
format_id. Choosing badly is itself a failure: a research finding forced into a
how-to becomes invented steps, and a tool announcement forced into a comparison
invents a rival. If none fits well, pick the least bad and lean on its spirit
rather than its letter.

{structures}

Write exactly the number of body beats listed for the structure you pick, plus
the hook and the payoff.

HARD RULES
- Total spoken words across every beat: {budget - 15} to {budget + 15}. This is a hard budget; the video is cut to length otherwise.
- Every factual claim must trace to the source material you are given. If the source does not state a number, do not state a number.
- If the source material is too thin to support a specific, non-obvious video, say so by making the hook voiceover exactly "INSUFFICIENT_SOURCE" and leave other fields short. Do not pad a weak topic.
- Captions are not subtitles. They are punchy on-screen fragments that add emphasis, not a transcript of the voiceover.
- Write for the ear. Short sentences. No semicolons, no parentheticals, no bulleted phrasing read aloud.

NEVER DO THESE
{forbidden}

THE HOOK
The first line decides whether the video is watched at all, and it is the single
weakest thing this channel produces. Rules, in order:

1. Open on the most CONCRETE thing in the source - something a person could
   picture, or a number attached to a real consequence. If the source contains
   something visually striking, that is the hook. Do not bury it in beat three.
2. Never open with a generalisation about people. "Most people...", "Everyone
   who...", "We all..." - these are the lowest-scoring openers there are. They
   describe a category, and nobody pictures a category.
3. Never restate the headline. If your hook is the title with different words,
   the viewer already scrolled.
4. Do not open with a bare statistic. A number with no stake attached - "a 350M
   model", "fourteen points better" - is abstract, not specific. Give the number
   something to be true ABOUT.
5. The hook must survive a fact check on its own. Making a line punchier by
   merging two different projects, people or results is the most common way
   these scripts become wrong. A hook that is vivid and false is worse than one
   that is flat and true - it fails harder in review, and it would be worse in
   public.

THE FAILURE TO AVOID
The single most common failure is a script that walks through the source in the
source's own order, using the source's own framing. That is a readback, not a
video. A viewer who skimmed the headline must learn something they could not
have guessed from it.

Before writing, find the ONE detail in the source that changes how a reader
thinks about the subject - a surprising number, an unintuitive tradeoff, a
comparison the author made in passing. Build the whole script around that. Every
script must contain at least one concrete specific - a figure, a named tool, a
measured difference - that appears in the body of the source and not in its
headline.

Do not narrate steps in order unless the ordering itself is the insight. A list
of setup instructions read aloud is the single lowest-performing thing you can
make. Name the tool. Name what it replaces. Name the tradeoff."""


def _describe(eligible: list[dict]) -> str:
    return "\n\n".join(
        f"  id: {f['id']}\n  {f['label']} - {f['beats']} body beats\n  {f['structure']}"
        for f in eligible
    )


def _source_block(topic: dict) -> str:
    return f"""SOURCE MATERIAL
Headline: {topic['title']}
Origin: {topic.get('source') or 'unknown'}
URL: {topic.get('url') or 'n/a'}
Summary: {topic.get('summary') or '(no summary text was available beyond the headline)'}"""


def eligible_formats(cfg: Any, store: Any) -> list[dict]:
    """Formats not used inside the cooldown window.

    Rotation exists to stop the channel looking mass-produced, but picking at
    random from it ignored whether the format suited the topic at all - a
    research finding drawn as a how-to forces the writer to invent steps.
    The cooldown still constrains the choice; the writer makes it.
    """
    variants = cfg.get("formats.variants", [])
    cooldown = int(cfg.get("formats.cooldown", 4))
    recent = set(store.recent_values("format_id", cooldown))
    return [v for v in variants if v["id"] not in recent] or variants


def pick_format(cfg: Any, store: Any) -> dict:
    """Kept for callers that want a single format without asking the model."""
    return random.choice(eligible_formats(cfg, store))


def write(client: anthropic.Anthropic, cfg: Any, topic: dict, eligible: list[dict],
          feedback: str = "") -> Script | None:
    """Generate one script. Returns None if the model declined or bailed."""
    user = _source_block(topic)
    if feedback:
        user += (
            "\n\nA previous attempt was rejected by review.\n"
            f"{feedback}\n"
            "Raise the weak dimension WITHOUT lowering the others - the last "
            "attempt fixed one score by breaking another. Keep what already "
            "scored well and change only what the review names."
        )

    resp = client.messages.parse(
        model=MODEL,
        max_tokens=8000,
        system=_system(cfg, eligible),
        messages=[{"role": "user", "content": user}],
        output_format=Script,
        thinking={"type": "adaptive"},
    )
    if resp.stop_reason == "refusal":
        print(f"  ! model declined this topic ({getattr(resp.stop_details, 'category', '?')})")
        return None

    script = resp.parsed_output
    if script.hook.voiceover.strip() == "INSUFFICIENT_SOURCE":
        print("  ! source too thin for a specific script")
        return None
    return script


def grade(client: anthropic.Anthropic, cfg: Any, topic: dict, script: Script) -> Grade:
    """Independent scoring pass. Deliberately given the source, not the prompt."""
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        system=(
            "You are a harsh reviewer for a short-form video channel. You are the last "
            "check before the video publishes automatically with no human involved. "
            "Score honestly and low by default; a merely competent script is a 60, not an 85. "
            "You are looking for reasons to reject, not reasons to approve."
        ),
        messages=[{
            "role": "user",
            "content": f"{_source_block(topic)}\n\nSCRIPT UNDER REVIEW\n{script.model_dump_json(indent=2)}",
        }],
        output_format=Grade,
        thinking={"type": "adaptive"},
    )
    return resp.parsed_output


def passes(cfg: Any, g: Grade) -> tuple[bool, str]:
    """Apply the configured floors and the aggregate minimum."""
    floors = cfg.get("quality.floors", {}) or {}
    # A floor naming a dimension the grader does not produce is a config typo.
    # Resolving it with a default silently either ignores the floor or blocks
    # every script; neither is discoverable. Fail loudly instead.
    unknown = set(floors) - set(Grade.model_fields)
    if unknown:
        raise ValueError(
            f"quality.floors names unknown dimension(s): {sorted(unknown)}. "
            f"Valid dimensions: {sorted(Grade.model_fields)}"
        )

    failed = [
        f"{dim}={getattr(g, dim)}<{int(floor)}"
        for dim, floor in floors.items()
        if getattr(g, dim) < int(floor)
    ]
    if failed:
        # Reporting only the first failure made the message contradict the
        # grader's own verdict, which names the genuinely weakest dimension.
        return False, f"below floor: {', '.join(failed)} | {g.verdict}"

    dims = [g.hook_strength, g.factual_grounding, g.originality, g.policy_safety]
    total = sum(dims) / len(dims)
    minimum = float(cfg.get("quality.min_score", 78))
    if total < minimum:
        return False, f"average {total:.2f} below minimum {minimum:.2f}: {g.verdict}"
    return True, f"passed at {total:.2f}"


def produce(client: anthropic.Anthropic, cfg: Any, store: Any,
            topic: dict) -> tuple[Script, dict, float] | None:
    """Write -> grade -> retry loop. Returns (script, format, score) or None."""
    eligible = eligible_formats(cfg, store)
    by_id = {f["id"]: f for f in eligible}
    feedback = ""

    for attempt in range(int(cfg.get("quality.max_retries", 2)) + 1):
        script = write(client, cfg, topic, eligible, feedback)
        if script is None:
            return None
        fmt = by_id.get(script.format_id) or eligible[0]
        print(f"  format chosen: {fmt['label']}")

        words = script.word_count()
        g = grade(client, cfg, topic, script)
        ok, reason = passes(cfg, g)
        avg = (g.hook_strength + g.factual_grounding + g.originality + g.policy_safety) / 4
        print(f"  attempt {attempt + 1}: {words}w | hook {g.hook_strength} "
              f"grounding {g.factual_grounding} orig {g.originality} "
              f"safety {g.policy_safety} -> {reason}")

        if ok:
            return script, fmt, avg
        # The full scorecard, not just one instruction, so the rewrite can see
        # which dimensions it must not sacrifice.
        feedback = (
            f"Scores - hook {g.hook_strength}, grounding {g.factual_grounding}, "
            f"originality {g.originality}, safety {g.policy_safety}.\n"
            f"Reviewer: {g.verdict}\n"
            f"Required fix: {g.fix or 'address the weakest dimension above.'}"
        )

    print("  ! failed review, skipping topic")
    return None
