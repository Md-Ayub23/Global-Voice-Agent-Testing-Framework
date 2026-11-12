"""The AI claim reader: what did the agent tell the caller about the result, when the patterns can't tell?

The outcome check reads the agent's words with the scenario's success and failure patterns. A reply
like "Lovely, that's all sorted for Friday then" matches neither list, so the outcome is NOT_VERIFIED
and the call proves nothing. This asks two different AI models to read the agent's replies after the
write call. Only when both give the same answer, and each quotes words that really are in the reply it
names, does that answer count; the outcome then says the claim was "read by AI". When they disagree,
or a quote isn't in the call, it stays unclear (NOT_VERIFIED, never PASS).

The reader never sees the tool answers or the database: it decides only what the agent SAID. Whether
that was true is still the outcome check's own comparison with what the tools did and what was stored.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Mapping, Sequence

from .contracts.models import LLMMessage, LLMRequest

SAID = ("success", "failure", "unclear", "nothing")

SYSTEM = """You read what an AI phone agent told a caller, for a release test. You get the action the
agent was asked to do and the agent's replies after it tried. Decide what the agent TOLD the caller about
that action, not whether it is true:
- success: the agent clearly said the action is done (booked, ordered, cancelled, saved, confirmed).
- failure: the agent clearly said it could not be done, or did not do it.
- nothing: the agent said nothing about whether this action happened.
- unclear: the words could mean either, or the agent said both.
Quote the exact words that decide it, copied from one reply, and give that reply's number.
A promise to do it later ("I'll book that now") is not success. Answer with JSON only."""


def _schema(seqs: Sequence[int]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["said", "reply", "quote"],
            "properties": {"said": {"type": "string", "enum": list(SAID)},
                           "reply": {"type": "integer", "enum": list(seqs)} if seqs else {"type": "integer"},
                           "quote": {"type": "string"}}}


def _prompt(question: Mapping[str, Any]) -> str:
    action = question["tool"].replace("_", " ")
    lines = [f"The action: {action} (the agent's tool {question['tool']})."]
    lines.append("The agent tried it." if question.get("wrote") else "The agent never called the tool.")
    lines.append("The agent's replies after that, numbered:")
    lines += [f"[{seq}] {text}" for seq, text in question["turns"]]
    return "\n".join(lines)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _check(answer: Mapping[str, Any], turns: Mapping[int, str]) -> dict[str, Any]:
    """One reader's answer, kept only when its quote is really in the reply it names."""
    said = answer.get("said") if answer.get("said") in SAID else "unclear"
    quote = str(answer.get("quote") or "").strip()
    seq = answer.get("reply")
    if said in ("success", "failure"):
        text = turns.get(seq) if isinstance(seq, int) else None
        if not text or not quote or _squash(quote) not in _squash(text):
            return {"said": "unclear", "seq": seq, "quote": quote, "note": "quote not found in the call"}
    return {"said": said, "seq": seq, "quote": quote}


async def read_claims(llm: Any, questions: Sequence[Mapping[str, Any]], models: Sequence[str],
                      metadata: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """{check id: {said, agreed, seq, quote, readers}} for each unclear outcome claim."""
    out: dict[str, dict[str, Any]] = {}
    for q in questions:
        turns = {seq: text for seq, text in q["turns"]}
        prompt = _prompt(q)

        async def ask(model: str) -> dict[str, Any]:
            r = await llm.complete(LLMRequest(
                purpose="claim_reader", model=model, system=SYSTEM, messages=(LLMMessage("user", prompt),),
                max_tokens=1500, metadata=dict(metadata or {}), json_schema=_schema(list(turns))))
            try:
                answer = json.loads(r.text)
            except ValueError:
                return {"said": "unclear", "model": r.model, "note": "the answer was not JSON"}
            return {**_check(answer if isinstance(answer, dict) else {}, turns), "model": r.model}

        answers = await asyncio.gather(*(ask(m) for m in models), return_exceptions=True)
        readers = []
        for m, a in zip(models, answers):
            if isinstance(a, BaseException):
                if type(a).__name__ == "BudgetExceeded":
                    raise a
                readers.append({"model": m, "said": "unclear", "note": f"{type(a).__name__}: {a}"})
            else:
                readers.append(a)
        says = {r["said"] for r in readers}
        agreed = len(readers) >= 2 and len(says) == 1 and "unclear" not in says
        said = readers[0]["said"] if agreed else "unclear"
        first = next((r for r in readers if r["said"] == said and r.get("seq") in turns), {})
        out[q["check_id"]] = {"said": said, "agreed": agreed, "seq": first.get("seq"),
                              "quote": first.get("quote") or "", "readers": readers}
    return out
