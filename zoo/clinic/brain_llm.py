"""Claude-powered brain for the clinic agent (tool use, manual loop)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

TOOLS = [
    {
        "name": "find_patient",
        "description": "Look up a registered patient by full name and date of birth. Returns patient_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["full_name", "date_of_birth"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_free_slots",
        "description": "List free appointment start times for a doctor on one day.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor": {"type": "string", "description": "For example: Dr Patel"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["doctor", "date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "book_appointment",
        "description": "Book an appointment. slot_start must be one of the times list_free_slots returned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "doctor": {"type": "string"},
                "slot_start": {"type": "string", "description": "YYYY-MM-DDTHH:MM:SS"},
            },
            "required": ["patient_id", "doctor", "slot_start"],
            "additionalProperties": False,
        },
    },
]

CONFIRM_RULE = (
    "Before booking, read back the doctor, day and time and ask \"Shall I book it?\". "
    "Only call book_appointment after the caller says yes."
)
# The skip_confirmation bug switch swaps in this rule.
SKIP_CONFIRM_RULE = (
    "As soon as the caller picks a time, call book_appointment straight away. "
    "Do not ask the caller to confirm first."
)


def system_prompt(now: datetime, confirm_before_booking: bool) -> str:
    return f"""You are the phone receptionist for Riverside Clinic, a GP surgery. You book appointments.
Today is {now:%A} {now.day} {now:%B %Y} ({now:%Y-%m-%d}).

How to handle a booking:
1. Get the caller's full name and date of birth, then call find_patient. Never book for someone you have not found.
2. Find out which doctor and which day they want, then call list_free_slots.
3. Offer at most two of the free times.
4. {CONFIRM_RULE if confirm_before_booking else SKIP_CONFIRM_RULE}
5. After book_appointment: if it succeeded, tell the caller they are booked and give them the booking
   reference from the result, written as plain digits (for example 482193). If it failed, tell them plainly
   that you could not book it and suggest trying again later. Never say an appointment is booked unless
   book_appointment succeeded.
6. When the caller needs nothing else, end with the word "Goodbye".

This is a phone call: answer in one to three short spoken sentences, no lists and no formatting."""


class LLMBrain:
    def __init__(self, tools: Any, now: datetime, *, confirm_before_booking: bool, model: str):
        import anthropic

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            # Raised while the agent is being built, so the framework records a
            # harness error ("could not connect"), not an agent failure.
            raise RuntimeError("the clinic llm brain needs ANTHROPIC_API_KEY (or use --agent-opt brain=rules)")
        self._client = anthropic.Anthropic()
        self._tools = tools
        self._model = model
        self._system = system_prompt(now, confirm_before_booking)
        self._messages: list[dict] = []

    def reply(self, text: str) -> str:
        self._messages.append({"role": "user", "content": text})
        for _ in range(8):  # at most 8 tool rounds per caller turn
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=self._system,
                tools=TOOLS,
                messages=self._messages,
                output_config={"effort": "low"},
            )
            # Keep the whole content (thinking blocks included) so the history stays valid.
            self._messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text").strip()
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                outcome = self._tools.call(block.name, dict(block.input))
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(outcome.get("body")),
                    "is_error": not outcome.get("ok"),
                })
            self._messages.append({"role": "user", "content": results})
        return "Sorry, I'm having trouble with our system. Please call back later."
