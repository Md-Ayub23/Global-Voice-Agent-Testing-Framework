"""LLM adapter (used by L3 caller, simulator validator and L7 judges)."""

from __future__ import annotations

from typing import Protocol

from .models import LLMRequest, LLMResponse


class LLMClient(Protocol):
    """One call to a language model.

    Implementations: Claude API adapter (reads ANTHROPIC_API_KEY), and a
    scripted fake for unit tests. Every request and response is logged to
    the call's llm/ folder, and identical requests are served from cache.
    Raises BudgetExceeded when the run's budget cap would be passed.
    """

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class BudgetExceeded(Exception):
    """The run's budget_usd limit was reached. Recorded as a harness error."""
