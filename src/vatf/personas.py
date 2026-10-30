"""The built-in persona library: ready-made callers any project can add (Set up > Test plan > Personas,
and the test plan writer in planner.py, which picks a persona for every test it writes).

These are behaviours to test against (an impatient caller, one who won't give details), not claims
about real groups of people. Each one is a normal persona file once added to a project, so it can be
edited there. Voices are the three local Piper voices Vocto downloads (two US English, one UK English).
"""

from __future__ import annotations

import copy
from typing import Any

US_1, US_2, UK = "en_US-lessac-medium", "en_US-amy-medium", "en_GB-alba-medium"


def _p(pid: str, title: str, description: str, traits: dict[str, str], voice: str, rate: float = 1.0,
       interrupts: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "0.1", "kind": "persona", "id": pid,
        "description": description,
        "traits": {"patience": "medium", "verbosity": "normal", "mood": "calm", "clarity": "high", **traits},
        "speech": {"tts_voice": voice, "speaking_rate": rate},
        "behaviour": {"interrupts": interrupts},
        "_title": title,
    }


LIBRARY: dict[str, dict[str, Any]] = {p["id"]: p for p in [
    _p("clear-caller", "Clear, polite caller",
       "A polite caller who says what they want plainly, answers each question once and waits for the agent "
       "to finish.", {}, US_1),
    _p("impatient-caller", "Impatient caller",
       "In a hurry. Gives short answers, sighs at extra questions, says things like \"can we speed this up\", "
       "but still answers when asked properly.", {"patience": "low", "verbosity": "terse", "mood": "frustrated"},
       US_2, 1.15),
    _p("angry-caller", "Angry caller",
       "Upset about an earlier bad experience and says so. Short-tempered at first; calms down only if the "
       "agent acknowledges the problem and helps. Never threatens anyone.",
       {"patience": "low", "mood": "angry"}, UK),
    _p("confused-caller", "Confused caller",
       "Not sure what they need. Gives partial or slightly wrong details at first, corrects them later, and "
       "asks what words mean.", {"clarity": "low", "mood": "anxious"}, US_1, 0.95),
    _p("rambling-caller", "Rambling caller",
       "Friendly and chatty. Tells a short side story before getting to the point and packs several details "
       "into one long sentence.", {"verbosity": "rambling", "clarity": "medium"}, UK),
    _p("slow-speaker", "Slow, careful speaker",
       "Speaks slowly with pauses in the middle of sentences, sometimes asks the agent to repeat or slow down, "
       "and likes to confirm each detail.", {"patience": "high", "clarity": "medium"}, US_2, 0.8),
    _p("fast-talker", "Fast talker",
       "Talks quickly and gives everything at once without waiting to be asked.",
       {"verbosity": "terse", "patience": "low"}, US_1, 1.35),
    _p("interrupter", "Caller who interrupts",
       "Cuts in when the agent gives a long answer, to add a detail or correct something.",
       {"patience": "low"}, US_2, 1.1, interrupts=True),
    _p("topic-switcher", "Caller who changes topic",
       "Starts with one request, switches to a different question halfway through, then comes back to the "
       "first one.", {"clarity": "medium"}, UK),
    _p("refuses-details", "Caller who won't give details",
       "Wary of sharing personal details. Asks why each one is needed and refuses at least once before giving "
       "it, or never gives it if no good reason is offered.", {"mood": "anxious", "clarity": "medium"}, US_1),
    _p("repeater", "Caller who repeats questions",
       "Asks the same question again in different words to make sure, even after it has been answered.",
       {"patience": "medium", "clarity": "medium"}, US_2),
    _p("second-language", "Caller speaking a second language",
       "English is not their first language. Uses short, simple sentences, sometimes the wrong word, and asks "
       "the agent to say things more simply.", {"clarity": "medium"}, UK, 0.9),
]}


def title(pid: str) -> str:
    return LIBRARY[pid]["_title"]


def persona_doc(pid: str) -> dict[str, Any]:
    """The persona file's contents (without the library's own title field)."""
    doc = copy.deepcopy(LIBRARY[pid])
    doc.pop("_title")
    return doc


def catalog() -> list[dict[str, Any]]:
    """For the dashboard: every library persona with its title and settings."""
    return [{"id": pid, "title": p["_title"], "description": p["description"], "traits": p["traits"],
             "speech": p["speech"], "behaviour": p["behaviour"]} for pid, p in LIBRARY.items()]
