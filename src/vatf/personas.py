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


def _p(pid: str, title: str, description: str, traits: dict[str, str], voice: str | None, rate: float = 1.0,
       interrupts: bool = False, behaviour: dict[str, Any] | None = None, speech: dict[str, Any] | None = None,
       use: str = "any") -> dict[str, Any]:
    """`use`: the test type the caller is made for (any, timing or speech); the test plan writer offers the
    timing and speech callers only when it writes those tests."""
    return {
        "schema_version": "0.1", "kind": "persona", "id": pid,
        "description": description,
        "traits": {"patience": "medium", "verbosity": "normal", "mood": "calm", "clarity": "high", **traits},
        "speech": {**({"tts_voice": voice} if voice else {}), "speaking_rate": rate, **(speech or {})},
        "behaviour": {"interrupts": interrupts, **(behaviour or {})},
        "_title": title, "_use": use,
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
    # Conversation & timing: how the caller takes turns (voice calls).
    _p("says-mm-hm", "Caller who says \"mm-hm\"",
       "Listens actively: says \"mm-hm\" or \"okay\" while the agent explains. The agent should keep going, "
       "not stop or start again.", {}, US_1, behaviour={"backchannels": True}, use="timing"),
    _p("coughs", "Caller who coughs (or a door bangs)",
       "Has a cold: coughs now and then while the agent is talking, and a door bangs once in the background. "
       "The agent should keep talking, not stop as if the caller had cut in.", {}, UK,
       behaviour={"noises": True, "noise_chance": 0.7}, use="timing"),
    _p("pauses-mid-sentence", "Caller who pauses mid-sentence",
       "Stops to think in the middle of longer sentences. The agent should wait for the end, not answer half a "
       "sentence.", {"patience": "high"}, US_2, 0.95, behaviour={"pauses_mid_sentence": True, "fillers": True}, use="timing"),
    _p("goes-quiet", "Caller who goes quiet",
       "Goes silent once in the call (looking for a card or a date). The agent should check in politely after a "
       "while, not hang up or keep talking over the silence.", {}, UK, behaviour={"goes_quiet": True}, use="timing"),
    # Speech & accents: how the caller sounds (voice calls).
    _p("quiet-caller", "Quiet, soft-spoken caller",
       "Speaks softly, as if on a bad headset. Says everything once, clearly but quietly.", {}, US_2,
       speech={"volume_db": -12}, use="speech"),
    _p("indian-english", "Caller with an Indian English accent",
       "Speaks fluent English with an Indian accent and spells names when asked.", {}, None,
       speech={"accent": "indian"}, use="speech"),
    _p("scottish-english", "Caller with a Scottish accent",
       "Speaks with a Scottish accent, quickly and naturally.", {}, None, 1.05, speech={"accent": "scottish"}, use="speech"),
    _p("spanish-speaker", "Spanish speaker using English",
       "A first-language Spanish speaker using English: an accent, short sentences, numbers said one by one.",
       {"clarity": "medium"}, None, 0.95, speech={"accent": "spanish-l1"}, use="speech"),
    _p("mandarin-speaker", "Mandarin speaker using English",
       "A first-language Mandarin speaker using English: an accent and some names spelled letter by letter.",
       {"clarity": "medium"}, None, 0.95, speech={"accent": "mandarin-l1"}, use="speech"),
]}


def for_type(test_type: str) -> list[str]:
    """The library callers the test plan writer may use for a test type: the general ones, plus the ones made
    for Timing or Speech when it writes those."""
    return [pid for pid, p in LIBRARY.items() if p["_use"] in ("any", test_type)]


def title(pid: str) -> str:
    return LIBRARY[pid]["_title"]


def persona_doc(pid: str) -> dict[str, Any]:
    """The persona file's contents (without the library's own title field)."""
    doc = copy.deepcopy(LIBRARY[pid])
    doc.pop("_title")
    doc.pop("_use")
    return doc


def catalog() -> list[dict[str, Any]]:
    """For the dashboard: every library persona with its title and settings."""
    return [{"id": pid, "title": p["_title"], "description": p["description"], "traits": p["traits"],
             "speech": p["speech"], "behaviour": p["behaviour"], "use": p["_use"]} for pid, p in LIBRARY.items()]
