"""Server-side chat — the conversation is a server concern, not a browser one.

The browser is a thin view: it POSTs a message, then attaches to an SSE stream
of the reply. The coach turn itself runs in a background task on the server, so
a page refresh, an app switch, or a flaky mobile connection never kills a reply
in progress — a reconnecting client resumes the stream from any character
offset and gets the rest.

Single athlete → a single conversation, persisted on the volume so history
survives restarts (a turn that was mid-flight at restart is lost; the client is
told so via the "gone" event and recovers from history).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from . import coach, config

log = logging.getLogger("coach.chat")

HISTORY_PATH = config.DATA_DIR / "chat" / "history.json"
TURN_TIMEOUT = 15 * 60      # hard cap on one coach turn, seconds
PING_INTERVAL = 15          # SSE keepalive so clients/proxies know we're alive

# History compaction — a sliding window so a week of chatting can't grow the
# file (or the page load) without bound. The coach's durable knowledge lives in
# its memory files, NOT here; old chat turns are safe to drop.
MAX_MESSAGES = 120          # newest messages kept
MAX_MSG_CHARS = 16_000      # a single stored message is truncated beyond this
MAX_TOTAL_CHARS = 120_000   # whole-history character budget
MIN_KEEP = 8                # always keep at least the latest few messages
MAX_TURN_CHARS = 300_000    # runaway-reply guard on the live turn buffer


class Turn:
    """One in-flight (or finished) coach reply, buffered server-side."""

    def __init__(self, turn_id: str) -> None:
        self.id = turn_id
        self.status = "running"          # running | done | error
        self.text = ""
        self.error: str | None = None
        self.started = time.monotonic()
        self.changed = asyncio.Event()   # pulsed on every appended chunk
        self.task: asyncio.Task | None = None
        self.discarded = False           # set by clear() — don't persist

    def snapshot(self) -> dict[str, Any]:
        # offsets/length are Python character counts; clients must treat them
        # as opaque and echo them back, never recompute from their own string.
        return {"id": self.id, "status": self.status, "length": len(self.text),
                "text": self.text, "error": self.error}

    def _wake(self) -> None:
        self.changed.set()
        self.changed.clear()


_messages: list[dict[str, str]] | None = None
_turn: Turn | None = None
_seq = 0


# --- persistence -------------------------------------------------------------
def _load() -> list[dict[str, str]]:
    global _messages
    if _messages is None:
        try:
            _messages = json.loads(HISTORY_PATH.read_text())["messages"]
        except (OSError, json.JSONDecodeError, KeyError):
            _messages = []
    return _messages


def _compact(msgs: list[dict[str, str]]) -> None:
    """Sliding-window compaction: cap single-message size, message count, and
    the total character budget (oldest dropped first)."""
    for m in msgs:
        if len(m.get("content") or "") > MAX_MSG_CHARS:
            m["content"] = m["content"][:MAX_MSG_CHARS] + "\n\n…[truncated]"
    del msgs[:-MAX_MESSAGES]
    total = sum(len(m.get("content") or "") for m in msgs)
    while len(msgs) > MIN_KEEP and total > MAX_TOTAL_CHARS:
        total -= len(msgs.pop(0).get("content") or "")


def _persist() -> None:
    """Persist history; never raises — a full/readonly volume must not take
    the chat down, it just loses durability until the disk recovers."""
    msgs = _load()
    _compact(msgs)
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"messages": msgs}))
        tmp.replace(HISTORY_PATH)
    except OSError as e:
        log.error("could not persist chat history: %s", e)


# --- public API ---------------------------------------------------------------
def state() -> dict[str, Any]:
    """History + the active turn (with its partial text) for page (re)loads."""
    turn = _turn.snapshot() if _turn else None
    return {"messages": list(_load()), "turn": turn,
            "onboarded": config.is_onboarded()}


async def send(message: str) -> dict[str, Any]:
    """Start a coach turn in the background. Refuses while one is running —
    the caller gets the running turn's snapshot and can attach to it instead
    (this is also what makes a client-side send-retry after a dropped POST
    safe: the retry just attaches to the turn the first attempt started)."""
    global _turn, _seq
    if _turn and _turn.status == "running":
        return {"ok": False, "error": "a reply is already in progress",
                "turn": _turn.snapshot()}
    _seq += 1
    turn = Turn(f"t{int(time.time())}-{_seq}")
    msgs = _load()
    history = list(msgs)                 # context = everything before this message
    msgs.append({"role": "user", "content": message})
    _persist()
    _turn = turn
    turn.task = asyncio.create_task(_run_turn(turn, message, history))
    return {"ok": True, "turn": turn.snapshot()}


async def clear() -> None:
    global _turn
    if _turn:
        _turn.discarded = True
        if _turn.task and not _turn.task.done():
            _turn.task.cancel()
        _turn = None
    _load().clear()
    _persist()


async def stream(turn_id: str, offset: int) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE events for a turn from `offset`: replay what's buffered, then
    follow live. Ends with error?/status/done. Unknown turn → single "gone"
    event (e.g. after a server restart) so the client falls back to history."""
    turn = _turn
    if turn is None or turn.id != turn_id:
        yield {"type": "gone"}
        return
    offset = max(0, min(int(offset), len(turn.text)))
    while True:
        if len(turn.text) > offset:
            delta = turn.text[offset:]
            offset = len(turn.text)
            yield {"type": "text", "text": delta, "offset": offset}
        if turn.status != "running":
            break
        try:
            await asyncio.wait_for(turn.changed.wait(), timeout=PING_INTERVAL)
        except asyncio.TimeoutError:
            yield {"type": "ping"}
    if turn.status == "error":
        yield {"type": "error", "text": turn.error or "coach failed"}
    # the coach may have updated week_plan.md / finished onboarding this turn
    yield {"type": "status", "onboarded": config.is_onboarded()}
    yield {"type": "done"}


# --- the background turn -------------------------------------------------------
async def _consume(turn: Turn, message: str, history: list[dict[str, str]]) -> None:
    async for chunk in coach.stream_reply(message, history):
        turn.text += chunk
        if len(turn.text) > MAX_TURN_CHARS:
            turn.text += "\n\n…[reply truncated — it exceeded the size limit]"
            log.warning("chat turn %s truncated at %d chars", turn.id, len(turn.text))
            break
        turn._wake()


async def _run_turn(turn: Turn, message: str, history: list[dict[str, str]]) -> None:
    try:
        await asyncio.wait_for(_consume(turn, message, history), timeout=TURN_TIMEOUT)
        turn.status = "done"
    except asyncio.TimeoutError:
        turn.status = "error"
        turn.error = f"coach timed out after {TURN_TIMEOUT // 60} minutes"
        log.error("chat turn %s timed out", turn.id)
    except asyncio.CancelledError:
        turn.status = "error"
        turn.error = "cancelled"
        raise
    except Exception as e:  # stream_reply already catches most — belt & braces
        turn.status = "error"
        turn.error = str(e)
        log.exception("chat turn %s failed", turn.id)
    finally:
        if not turn.discarded and turn.text:
            _load().append({"role": "assistant", "content": turn.text})
            _persist()
        turn.changed.set()   # release any streamer waiting on the final state
