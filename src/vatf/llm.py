"""LLM clients (contracts/llm.py): the Claude API adapter and a scripted fake for tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Sequence

from .contracts.llm import BudgetExceeded
from .contracts.models import LLMRequest, LLMResponse
from .util import write_json

# US dollars per million tokens (input, output). Unknown models are logged at cost 0.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
DEFAULT_MODEL = "claude-haiku-5-5"


class LLMError(Exception):
    """The model could not answer (refusal, API error). Recorded as a harness error."""


class Budget:
    """The run's spending cap (profile limits.budget_usd)."""

    def __init__(self, limit_usd: float):
        self.limit_usd = limit_usd
        self.spent_usd = 0.0

    def check(self) -> None:
        if self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(f"budget of ${self.limit_usd:.2f} used up (${self.spent_usd:.4f} spent)")

    def charge(self, cost: float) -> None:
        self.spent_usd += cost


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * inp + output_tokens * out) / 1_000_000


def log_exchange(request: LLMRequest, response: LLMResponse) -> None:
    """Every request and response goes to the call's llm/ folder (metadata log_dir)."""
    log_dir = request.metadata.get("log_dir")
    if not log_dir:
        return
    folder = Path(log_dir)
    folder.mkdir(parents=True, exist_ok=True)
    n = len(list(folder.glob("*.json"))) + 1
    meta = {k: v for k, v in request.metadata.items() if k != "log_dir"}
    write_json(folder / f"{n:03d}-{request.purpose}.json", {
        "request": {**asdict(request), "metadata": meta},
        "response": asdict(response),
    })


class ClaudeClient:
    """Calls Claude through the official SDK. Reads ANTHROPIC_API_KEY from the environment.

    Only judge requests (Phase 3) are cached: caching the caller would make every
    repeat of a scenario replay the same conversation and defeat pass^k.
    """

    def __init__(self, budget: Budget, cache_dir: Path | None = None, effort: str = "low"):
        import anthropic

        self._client = anthropic.AsyncAnthropic()
        self._budget = budget
        self._cache_dir = cache_dir
        self._effort = effort

    @property
    def spent_usd(self) -> float:
        return self._budget.spent_usd

    def request_kwargs(self, request: LLMRequest) -> dict:
        # No temperature: current Claude models reject non-default sampling settings.
        return {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "output_config": {"effort": self._effort},
        }

    async def complete(self, request: LLMRequest) -> LLMResponse:
        import anthropic

        kwargs = self.request_kwargs(request)
        cache_file = None
        if self._cache_dir and request.purpose.startswith("judge"):
            key = hashlib.sha256(json.dumps(kwargs, sort_keys=True).encode("utf-8")).hexdigest()
            cache_file = self._cache_dir / f"{key}.json"
            if cache_file.exists():
                cached = LLMResponse(**json.loads(cache_file.read_text(encoding="utf-8")))
                hit = LLMResponse(cached.text, cached.model, cached.input_tokens, cached.output_tokens,
                                  0.0, from_cache=True)
                log_exchange(request, hit)
                return hit

        self._budget.check()
        try:
            msg = await self._client.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"Claude API error: {e}") from e
        if msg.stop_reason == "refusal":
            raise LLMError("the model declined the request")
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        cost = price(msg.model, msg.usage.input_tokens, msg.usage.output_tokens)
        self._budget.charge(cost)
        if not text:
            # Usually max_tokens was used up by thinking before any answer was written.
            raise LLMError(f"empty answer from {msg.model} (stop_reason={msg.stop_reason}, "
                           f"{msg.usage.output_tokens} output tokens)")
        response = LLMResponse(text, msg.model, msg.usage.input_tokens, msg.usage.output_tokens, cost)
        log_exchange(request, response)
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            write_json(cache_file, asdict(response))
        return response


class ScriptedLLM:
    """Fake LLM for tests. Give it a list of replies, or a function of the request."""

    def __init__(self, replies: Sequence[str] | Callable[[LLMRequest], str]):
        self._replies = replies if callable(replies) else list(replies)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if callable(self._replies):
            text = self._replies(request)
        elif self._replies:
            text = self._replies.pop(0)
        else:
            raise LLMError("ScriptedLLM has no replies left")
        response = LLMResponse(text, "scripted", 0, 0, 0.0)
        log_exchange(request, response)
        return response
