"""The agent's tool client: plain HTTP calls to wherever its tools are configured to go.

For a test run that is Vocto's tool gateway (http://127.0.0.1:8790/tools, the default), which
records each call, can inject a fault, and forwards it to the restaurant's order API. To talk to
the agent without Vocto, set TOOLS_URL=http://127.0.0.1:7870/tools so calls go straight to the API.

Each call carries the test id Vocto passed in the call's start message (X-Test-Id), the way a
production agent passes a request id to its back end.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

import aiohttp
from loguru import logger

TOOLS_URL = os.getenv("TOOLS_URL", "http://127.0.0.1:8790/tools").rstrip("/")
TIMEOUT_S = float(os.getenv("TOOLS_TIMEOUT_S", "8"))

# Set by bot.py for each call, from the Twilio start message's customParameters.
call_params: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("call_params", default={})


async def call(name: str, **args: Any) -> dict[str, Any]:
    """Returns {"ok": bool, "status": int, "body": ...}; never raises, so a failure reaches the LLM."""
    params = call_params.get()
    headers = {"Content-Type": "application/json"}
    if params.get("test_id"):
        headers["X-Test-Id"] = params["test_id"]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as session:
            async with session.post(f"{TOOLS_URL}/{name}", json=args, headers=headers) as resp:
                status = resp.status
                try:
                    body: Any = await resp.json(content_type=None)
                except ValueError:
                    body = await resp.text()
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f"call {params.get('test_id', '-')} tool {name} failed: {type(e).__name__}: {e}")
        return {"ok": False, "status": 503, "body": {"error": "the order system did not answer"}}
    ok = 200 <= status < 300 and isinstance(body, dict)
    logger.info(f"call {params.get('test_id', '-')} tool {name} -> {status}")
    return {"ok": ok, "status": status, "body": body}
