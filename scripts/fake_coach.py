"""Fault-injection stand-in for app/coach.py, used by scripts/smoke_test.sh.

Mounted over /srv/app/coach.py in the second smoke-test phase. Streams a slow,
deterministic reply so the test can drop the connection mid-turn and prove the
server keeps the turn alive and the stream resumes at the right offset.
"""
import asyncio

SDK_AVAILABLE = True


async def stream_reply(message, history):
    for i in range(40):
        await asyncio.sleep(0.25)   # ~10s total
        yield f"chunk-{i:02d} "
