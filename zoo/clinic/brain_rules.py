"""Deterministic brain for the clinic agent.

A small state machine that pulls names, dates, doctors and times out of the
caller's words with regular expressions. It is free and repeatable, so the
framework's own tests can run the whole pipeline offline. It is not meant
to be clever: callers who phrase things very differently will confuse it,
which is exactly what a weak real agent does too.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .agent import GOODBYE, say_day, say_time

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
MINUTE_WORDS = {"o'clock": 0, "oclock": 0, "fifteen": 15, "thirty": 30, "forty-five": 45, "forty five": 45}
NOT_NAMES = {"hi", "hello", "yes", "yeah", "no", "dr", "doctor", "next", "thanks", "thank", "sure",
             "okay", "ok", "good", "great", "i", "it", "my", "that", "the", "this", "please", "morning",
             "afternoon", "sorry", "um", "well", "so"}

YES = re.compile(r"\b(yes|yeah|yep|yup|sure|please do|go ahead|correct|that's right|that is right|"
                 r"sounds good|perfect|book it)\b", re.I)
NO = re.compile(r"^\s*(no|nope|not really|actually no)\b", re.I)
BYE = re.compile(r"\b(bye|goodbye|that's all|that is all|nothing else|no thanks|no, thanks|no thank you)\b", re.I)


def find_name(text: str, asked: bool) -> str | None:
    m = re.search(r"\b(?i:i'm|i am|my name is|my name's|this is|name is|it's|it is)\s+"
                  r"([A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)+)", text)
    if not m and asked:
        m = re.match(r"\s*(?:yes,?\s+|sure,?\s+)?([A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)+)", text)
    if not m:
        return None
    words = m.group(1).split()
    while words and words[-1].lower() in NOT_NAMES:
        words.pop()
    if len(words) < 2 or words[0].lower() in NOT_NAMES:
        return None
    return " ".join(words)


def find_dob(text: str) -> str | None:
    m = re.search(r"\b(19\d{2}|20\d{2})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]+),?\s+(\d{4})\b", text)
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text)
    if m and m.group(1).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def find_doctor(text: str) -> str | None:
    m = re.search(r"\b(?:dr\.?|doctor)\s+([A-Za-z][a-z'-]+)", text, re.I)
    return f"Dr {m.group(1).capitalize()}" if m else None


def find_day(text: str, now: datetime) -> str | None:
    low = text.lower()
    if "tomorrow" in low:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if "today" in low:
        return now.strftime("%Y-%m-%d")
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", low):
            ahead = (i - now.weekday()) % 7 or 7
            return (now + timedelta(days=ahead)).strftime("%Y-%m-%d")
    return None


def find_part_of_day(text: str) -> str | None:
    low = text.lower()
    if "morning" in low:
        return "morning"
    if "afternoon" in low:
        return "afternoon"
    return None


def find_time(text: str) -> tuple[int, int | None] | None:
    """Returns (hour 0-23, minute or None)."""
    low = text.lower()
    m = re.search(r"\bhalf (?:past )?(\w+)\b", low)
    if m and m.group(1) in NUMBER_WORDS:
        return _to_24h(NUMBER_WORDS[m.group(1)], low), 30
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d/-])", low)
    if m and (m.group(2) or m.group(3)) and 1 <= int(m.group(1)) <= 23:
        hour = int(m.group(1))
        if m.group(3) and m.group(3).startswith("p") and hour < 12:
            hour += 12
        elif not m.group(3):
            hour = _to_24h(hour, low)
        return hour, int(m.group(2)) if m.group(2) else None
    words = "|".join(NUMBER_WORDS)
    minutes = "|".join(MINUTE_WORDS)
    m = re.search(rf"\b({words})(?:\s+({minutes}))?\b", low)
    if m and (m.group(2) or re.search(r"\b(at|works|please|am|pm)\b", low)):
        return _to_24h(NUMBER_WORDS[m.group(1)], low), MINUTE_WORDS.get(m.group(2) or "", None)
    return None


def _to_24h(hour: int, low: str) -> int:
    if "pm" in low or "afternoon" in low:
        return hour + 12 if hour < 12 else hour
    return hour + 12 if hour < 8 else hour  # clinic hours: "two" means 2pm


class RulesBrain:
    def __init__(self, tools: Any, now: datetime, *, confirm_before_booking: bool):
        self.tools = tools
        self.now = now
        self.confirm = confirm_before_booking
        self.name = self.dob = self.patient_id = None
        self.doctor = self.day = self.part = None
        self.offered: list[str] = []
        self.chosen: str | None = None
        self.awaiting_confirm = False
        self.asked_identity = False
        self.done = False  # booking finished (either way); now "anything else?"

    def reply(self, text: str) -> str:
        if self.done:
            if BYE.search(text) or NO.search(text):
                return GOODBYE
            return "I can only help with booking appointments today. Is there anything else?"

        if self.awaiting_confirm:
            if NO.search(text):
                self.awaiting_confirm, self.chosen = False, None
                return "No worries. Which time would suit you instead?"
            if YES.search(text):
                return self._book()

        self.name = self.name or find_name(text, self.asked_identity)
        self.dob = self.dob or find_dob(text)
        self.doctor = find_doctor(text) or self.doctor
        new_day = find_day(text, self.now)
        if new_day and new_day != self.day:
            self.day, self.offered, self.chosen = new_day, [], None
        self.part = find_part_of_day(text) or self.part
        wanted_time = find_time(text)

        if not self.patient_id:
            if not self.name or not self.dob:
                self.asked_identity = True
                missing = "full name and date of birth" if not (self.name or self.dob) else (
                    "full name" if not self.name else "date of birth")
                return f"Can I have your {missing}, please?"
            found = self.tools.call("find_patient", {"full_name": self.name, "date_of_birth": self.dob})
            if not found.get("ok"):
                self.name = self.dob = None
                return ("Sorry, I can't find a patient with that name and date of birth. "
                        "Could you give me your full name and date of birth again?")
            self.patient_id = found["body"]["patient_id"]

        if not self.doctor:
            return "Which doctor would you like to see?"
        if not self.day:
            return f"Which day would you like to see {self.doctor}?"

        if not self.offered:
            free = self.tools.call("list_free_slots", {"doctor": self.doctor, "date": self.day})
            if not free.get("ok"):
                return "Sorry, I couldn't check the diary just now. Could you try a different day?"
            slots = free["body"]["slots"]
            if not slots:
                self.day = None
                return f"{self.doctor} has no free times that day. Which other day would suit you?"
            self.offered = slots
            if wanted_time is None:
                return self._offer()

        if wanted_time is not None:
            match = self._match(wanted_time)
            if match is None:
                return "Sorry, that time isn't free. " + self._offer()
            self.chosen = match
        elif len(self.offered) == 1 and YES.search(text):
            self.chosen = self.offered[0]
        elif re.search(r"\b(first|earliest|that one|the earlier)\b", text, re.I):
            self.chosen = self._shortlist()[0]

        if not self.chosen:
            return self._offer()
        if not self.confirm:
            return self._book()
        self.awaiting_confirm = True
        return (f"So that's {self.doctor} on {say_day(self.chosen)} at {say_time(self.chosen)}. "
                "Shall I book it?")

    def _shortlist(self) -> list[str]:
        if self.part == "morning":
            picks = [s for s in self.offered if int(s[11:13]) < 12]
        elif self.part == "afternoon":
            picks = [s for s in self.offered if int(s[11:13]) >= 12]
        else:
            picks = []
        return (picks or self.offered)[:2]

    def _offer(self) -> str:
        times = [say_time(s) for s in self._shortlist()]
        intro = "Thanks. " if self.name else ""
        listed = " or ".join(times)
        return f"{intro}{self.doctor} has {listed} on {say_day(self.offered[0])}. Which would you like?"

    def _match(self, wanted: tuple[int, int | None]) -> str | None:
        hour, minute = wanted
        for slot in self.offered:
            if int(slot[11:13]) == hour and (minute is None or int(slot[14:16]) == minute):
                return slot
        return None

    def _book(self) -> str:
        self.awaiting_confirm = False
        self.done = True
        result = self.tools.call("book_appointment", {
            "patient_id": self.patient_id, "doctor": self.doctor, "slot_start": self.chosen})
        if result.get("ok"):
            return (f"You're booked with {self.doctor} on {say_day(self.chosen)} at {say_time(self.chosen)}. "
                    "Is there anything else I can help with?")
        return ("I'm sorry, I couldn't book that appointment because our booking system isn't responding. "
                "Please try again later. Is there anything else I can help with?")
