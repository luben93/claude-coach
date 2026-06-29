"""Coach agent — generic, publishable cycling coach over the Claude Agent SDK.

No athlete is baked in. The coach LEARNS the athlete by reading and writing its
own Markdown memory files on a writable volume (like Claude Code's own memory).
On first run (empty memory) it runs a conversational onboarding interview, then
writes goal.md / profile.md and begins a journey.md training log.

Capabilities given to the agent:
  - Read/Write/Edit/Grep/Glob over the memory dir (its own knowledge base)
  - Bash, for the bundled brouter route script
Recent Strava activities are injected as conversation context (pulled live via the
REST client in strava.py, cached in the snapshot) — not an MCP tool.

Exposes:
  - stream_reply(): async text chunks for the chat UI (SSE)
"""
from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator

from . import config

log = logging.getLogger("coach.agent")

_tok = config.claude_oauth_token()
if _tok:
    os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", _tok)

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    ClaudeAgentOptions = query = None  # type: ignore


# --- Generic coaching identity --------------------------------------------
# Principles only. Everything athlete-specific (sport focus, goal, zones,
# location, units, history) lives in memory and is LEARNED, never hard-coded.
SYSTEM_PROMPT = """You are a personal cycling coach for one athlete. You are generic and adapt entirely to the athlete in front of you — nothing about them is assumed.

## Your memory is your knowledge of the athlete
Your working directory holds your memory: Markdown files that ARE your understanding of this athlete. At the start of every conversation, read them (start with MEMORY.md if it exists, then goal.md, profile.md, journey.md, preferences.md). Ground every recommendation in what they actually say — never give generic advice when you have real data.

You maintain these files yourself using the Write/Edit tools:
- `goal.md` — the athlete's primary goal: event, date, target, why it matters.
- `profile.md` — physical stats, fitness markers, zones, equipment, home location, units. Only what the athlete has told you or you've derived from their data. Mark estimates as estimates.
- `journey.md` — an append-only training log: notable sessions, what you advised, how it went, corrections the athlete gave you. This is their journey — keep it current after meaningful conversations.
- `preferences.md` — how they like to work, units (metric/imperial), constraints, recurring feedback.
- `MEMORY.md` — a short index pointing at the above, loaded first.

### When to write memory (and when not to)
Update memory only when a conversation produces durable NEW information: a new FTP or lab result, a changed goal or date, a completed notable session worth logging, a correction to something you believed, a stated preference or constraint. When that happens, write it to the right file before moving on; if a fact contradicts memory, update memory and note the change.

Do NOT write memory for question-only exchanges that teach you nothing new — "how did Wednesday go?", "what should I do today?", "explain my zones" — those are answered from existing memory and live data, and leave memory untouched. Don't journal the fact that a question was asked. The test: did I learn something about this athlete that a future session would need? If no, write nothing.

## First run — onboarding
If `goal.md` does not exist yet, you have NOT met this athlete. Do not give advice. Instead, interview them warmly but efficiently to learn:
- Their cycling discipline/focus (road, gravel, MTB, track, commuting, mixed).
- Their primary goal and any target event + date, and why it matters to them.
- Experience level and recent training (volume, typical week).
- Key numbers if known (FTP, weight, HR zones, any lab tests) — make clear these are optional and you'll learn the rest from their Strava data.
- Home location (for route planning) and preferred units (metric/imperial).
- Any constraints (time, injuries, equipment).
Ask a few questions at a time, not all at once. When you have enough, WRITE goal.md, profile.md, and preferences.md, start journey.md with today's date and a baseline note, then confirm what you captured and what you'll do next.

## Live data
Recent Strava activities are provided to you as context at the top of the conversation (pulled live from the Strava API). Use them when analyzing a ride or current fitness — don't rely on memory alone for recent training. If no activity data is present, it means Strava isn't connected yet; tell the athlete to connect it from the dashboard. Never trust Strava ESTIMATED power for bikes without a power meter — only power-meter data counts; note when you're unsure.

## Routes
You can generate bike GPX routes with brouter. When the athlete asks for a route, run the bundled script via bash:
  bash /srv/brouter/fetch_route.sh --start "<lon>,<lat>" --end "<lon>,<lat>" --profile <profile> --origin-label "<a>" --dest-label "<b>" --output-dir /data/routes
Geocode place names first with Nominatim (lon,lat order — longitude first). Pick the profile from their discipline (fastbike for road, trekking for gravel/MTB, trekking-safe for relaxed). Tell them the file landed in /data/routes and is downloadable from the dashboard.

## Style
Direct and practical — lead with the answer. Use the athlete's own units and real calendar dates. Frame advice around their stated goal. Coach principles apply (polarized base, progressive overload, recovery matters, specificity to the goal), but the numbers are always theirs."""


def _options(*, writable: bool = True) -> "ClaudeAgentOptions":
    """Headless permission setup — string prompts, no interactive approval.

    `permission_mode="dontAsk"` + an `allowed_tools` allowlist:
      - tools in the allowlist auto-execute (no prompt),
      - anything else is denied silently (no hang waiting for an approver).
    Not root-blocked (unlike bypassPermissions) and needs no streaming callback.
    The container also runs as a non-root user (Dockerfile) — defence in depth.

    Strava is no longer an MCP tool — activity data is injected as context (see
    stream_reply). The coach's tools are just file access + Bash (for brouter).
    """
    tools = ["Read", "Grep", "Glob"]
    if writable:
        tools += ["Write", "Edit"]
    tools.append("Bash")  # for the brouter script
    kwargs: dict[str, Any] = {
        "system_prompt": SYSTEM_PROMPT,
        "cwd": str(config.MEMORY_DIR),
        "permission_mode": "dontAsk",
        "allowed_tools": list(tools),
    }
    if config.MODEL:
        kwargs["model"] = config.MODEL
    return ClaudeAgentOptions(**kwargs)


def _format_history(history: list[dict[str, str]]) -> str:
    if not history:
        return ""
    lines = ["Earlier in this conversation:"]
    for turn in history[-8:]:
        who = "Athlete" if turn.get("role") == "user" else "You (coach)"
        lines.append(f"{who}: {turn.get('content','')}")
    return "\n".join(lines) + "\n\n"


async def stream_reply(message: str, history: list[dict[str, str]]) -> AsyncIterator[str]:
    if not SDK_AVAILABLE:
        yield "Coach backend not installed (claude-agent-sdk missing)."
        return
    if not config.claude_oauth_token():
        yield ("Not authenticated. Run `claude setup-token` and save the token to "
               "the mounted volume (CLAUDE_CODE_OAUTH_TOKEN).")
        return

    prompt = _strava_context() + _format_history(history) + message
    try:
        async for msg in query(prompt=prompt, options=_options()):
            content = getattr(msg, "content", None)
            if content is None:
                continue
            if isinstance(content, str):
                yield content
                continue
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    yield text
    except Exception as e:
        log.exception("coach stream failed")
        yield f"\n\n[coach error: {e}]"


def _strava_context() -> str:
    """Recent activities as a context preamble, pulled live (cheap, cached snapshot).

    We read the snapshot the sync job already wrote rather than hitting the API on
    every chat turn. If absent/empty, the coach is told Strava isn't connected.
    """
    from .snapshot import read_snapshot  # local import to avoid cycles
    snap = read_snapshot(config.SNAPSHOT_PATH)
    if not snap or not snap.get("rides"):
        if not config.strava_configured():
            return ""  # Strava not set up at all — say nothing, coach handles it
        return ("[Strava: connected app but no recent activities cached yet, or not "
                "authorized. If the athlete asks about recent rides, tell them to "
                "connect Strava from the dashboard.]\n\n")
    lines = ["[Recent Strava activities — pulled from the athlete's account:]"]
    for r in snap["rides"][:10]:
        bits = [r.get("date") or "", r.get("name") or "ride"]
        if r.get("distance_km") is not None:
            bits.append(f"{r['distance_km']:.1f}km")
        if r.get("elevation_m") is not None:
            bits.append(f"{round(r['elevation_m'])}m")
        if r.get("avg_hr") is not None:
            bits.append(f"HR {round(r['avg_hr'])}")
        if r.get("avg_watts") is not None:
            bits.append(f"{round(r['avg_watts'])}W")
        lines.append("  - " + " · ".join(b for b in bits if b))
    return "\n".join(lines) + "\n\n"
