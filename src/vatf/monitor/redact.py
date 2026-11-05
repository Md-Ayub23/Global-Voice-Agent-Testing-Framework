"""Removes personal data from a real call before anything is written to disk (Phase 8).

Real calls hold real customers' data, so the original text never reaches the store: intake hands the
call here, and only the redacted copy is saved, scored and shown (and sent to the AI judges).

What is removed, each replaced by a label such as [phone]:
  email        jane@example.com
  card         13-19 digit numbers that pass the Luhn check (payment cards)
  id_number    US SSN 123-45-6789, Indian Aadhaar 1234 5678 9012 and PAN ABCDE1234F, UK NI QQ123456C
  bank         IBANs
  phone        9-15 digit numbers (with + ( ) - . or spaces)
  address      "221 Baker Street", "12 MG Road", UK postcodes
  birth_date   a date near "born", "birthday", "date of birth", "DOB"
  name         names after "my name is", "this is", "I'm", "call me" - and those names everywhere else in
               the call
Always, whatever is switched off: the profile's pii.mask_patterns, the owner's own monitor patterns, and
personal tool arguments (the tools' pii_args, plus common keys such as email, phone, date_of_birth, and
`name` in what the agent sends to a tool). A personal tool argument is kept as a salted hash
([masked:ab12cd34ef56]) and its value is also removed wherever it is said in the call.
The caller's phone number becomes a one-way hash, so repeat callers can be counted without storing it.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from ..util import mask_token
from .settings import DETECTORS

MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
         r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
DATE = (rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|"
        rf"(?:the\s+)?\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH},?\s+\d{{4}}|"
        rf"{MONTH}\s+(?:the\s+)?\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})")
STREET = (r"street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd|way|court|ct|place|pl|close|"
          r"crescent|terrace|parkway|highway|hwy|nagar|marg")
NOT_NAMES = {"sorry", "calling", "looking", "not", "just", "here", "fine", "good", "great", "sure", "okay", "ok",
             "yes", "no", "the", "a", "an", "trying", "going", "interested", "afraid", "happy", "still", "very",
             "so", "actually", "really", "thank", "thanks", "hoping", "wondering", "phoning", "ringing", "about",
             "on", "in", "at", "with", "from", "your", "you", "my", "it", "that", "this", "what", "how", "glad",
             "pleased", "back", "done", "ready", "available", "new", "an", "having", "after", "booked", "unable",
             "able", "also", "now", "speaking", "hello", "hi", "hey", "well", "correct", "right", "free", "busy"}

PATTERNS: dict[str, list[re.Pattern]] = {
    "email": [re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")],
    "card": [re.compile(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])")],
    "id_number": [re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), re.compile(r"(?<!\d)(?<!\d[ -])\d{4}[ -]\d{4}[ -]\d{4}(?![ -]?\d)"),
                  re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
                  re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2} ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b")],
    "bank": [re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")],
    "phone": [re.compile(r"(?<![\w+])\+?\(?\d[\d ().-]{7,}\d(?![\w])")],
    "address": [re.compile(rf"\b\d{{1,5}}[a-z]?,?\s+(?:[a-z0-9'.-]+\s+){{1,3}}(?:{STREET})\b\.?", re.I),
                re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b")],
    "birth_date": [re.compile(rf"(?P<ctx>\b(?:born|birthday|birth\s*date|date\s+of\s+birth|dob|d\.o\.b\.?)\b[^.?!\n]{{0,25}}?)"
                              rf"(?P<v>{DATE})", re.I)],
}
INTRO = re.compile(r"(?i:\b(?:my name is|my name's|name is|this is|i am|i'm|im|call me|it's|speaking with|"
                   r"you're speaking to|you are speaking to))\s+(?P<n>[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,2})")
PII_KEYS = {"first_name", "last_name", "full_name", "surname", "caller_name", "customer_name", "patient_name",
            "email", "email_address", "phone", "phone_number", "mobile", "telephone", "dob", "date_of_birth",
            "birth_date", "birthday", "address", "street", "street_address", "postcode", "zip", "zip_code",
            "postal_code", "ssn", "card", "card_number", "iban", "account_number", "passport", "national_id",
            "aadhaar", "pan"}
REQUEST_ONLY_KEYS = {"name"}         # `name` sent to a tool is usually the caller; in a tool's answer, a product


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _norm_key(k: str) -> str:
    return re.sub(r"[\s-]+", "_", re.sub(r"(?<=[a-z])(?=[A-Z])", "_", str(k))).lower()


def caller_hash(number: Any, salt: str) -> str | None:
    """A one-way hash of the caller's number (digits only): the same caller gets the same hash."""
    digits = re.sub(r"\D", "", str(number or ""))
    if len(digits) < 5:
        return None
    return hashlib.sha256(f"{salt}|caller|{digits[-10:]}".encode()).hexdigest()[:16]


class Redactor:
    """One call's redaction: built from the call's texts and tool arguments, then applied to every field."""

    def __init__(self, profile: Mapping[str, Any], privacy: Mapping[str, Any], salt: str):
        self.salt = salt
        self.on = [d for d in DETECTORS if d in set(privacy.get("redact") or [])]
        self.keys = PII_KEYS | {_norm_key(k) for t in profile.get("tools", []) for k in t.get("pii_args", [])}
        self.own = [(p["name"], re.compile(p["regex"])) for p in
                    list((profile.get("pii") or {}).get("mask_patterns", [])) + list(privacy.get("patterns") or [])
                    if p.get("name") and p.get("regex")]
        self.known: list[tuple[str, re.Pattern]] = []
        self.counts: Counter[str] = Counter()

    # ---- learning the call's personal values ----
    def learn(self, texts: Iterable[str], tool_args: Iterable[Mapping[str, Any]]) -> None:
        """Personal values said in the call (introduced names) and sent to tools, to remove everywhere."""
        values: list[tuple[str, str]] = []
        if "name" in self.on:
            for t in texts:
                for m in INTRO.finditer(t or ""):
                    words = m.group("n").split()
                    while words and words[-1].lower() in NOT_NAMES:
                        words.pop()
                    if words and words[0].lower() not in NOT_NAMES:
                        values.append(("name", " ".join(words)))
        for args in tool_args:
            for k, v in _walk(args):
                nk = _norm_key(k)
                if (nk in self.keys or nk in REQUEST_ONLY_KEYS) and isinstance(v, (str, int)) and len(str(v).strip()) >= 3:
                    values.append(("name" if "name" in nk else nk, str(v).strip()))
        whole, words = [], []
        seen = set()
        for label, v in values:
            if v.lower() in seen:
                continue
            seen.add(v.lower())
            whole.append((len(v), label, re.compile(rf"(?<!\w){re.escape(v)}(?!\w)", re.I)))
            parts = [w for w in re.split(r"\s+", v) if len(w) >= 3 and w.lower() not in NOT_NAMES]
            if label == "name" and len(parts) > 1:
                words.append((label, re.compile(r"\b(?:" + "|".join(map(re.escape, parts)) + r")\b", re.I)))
        whole.sort(key=lambda x: -x[0])          # whole values first, longest first, then single name words
        self.known = [(lb, p) for _, lb, p in whole] + words

    # ---- applying ----
    def _sub(self, label: str, pattern: re.Pattern, text: str, check=None) -> str:
        def repl(m: re.Match) -> str:
            if "v" in pattern.groupindex:         # only the value is removed, its context stays
                self.counts[label] += 1
                whole, at = m.group(0), m.start()
                return whole[:m.start("v") - at] + f"[{label}]" + whole[m.end("v") - at:]
            if check and not check(m.group(0)):
                return m.group(0)
            self.counts[label] += 1
            return f"[{label}]"
        return pattern.sub(repl, text)

    def text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        if "email" in self.on:                    # before names: "maria.lopez@..." is one email, not two names
            for p in PATTERNS["email"]:
                text = self._sub("email", p, text)
        for label, p in self.known:
            text = self._sub(label, p, text)
        for label, p in self.own:
            text = self._sub(label, p, text)
        for d in self.on:
            if d == "email":
                continue
            for p in PATTERNS.get(d, []):
                if d == "card":
                    text = self._sub(d, p, text, lambda s: _luhn(re.sub(r"\D", "", s)))
                elif d == "phone":
                    text = self._sub(d, p, text, lambda s: 9 <= len(re.sub(r"\D", "", s)) <= 15)
                else:
                    text = self._sub(d, p, text)
        return text

    def value(self, node: Any, request: bool = False) -> Any:
        """Tool arguments and answers: personal keys as salted hashes, every other string redacted."""
        if isinstance(node, Mapping):
            out = {}
            for k, v in node.items():
                nk = _norm_key(k)
                if (nk in self.keys or (request and nk in REQUEST_ONLY_KEYS)) and v is not None \
                        and not isinstance(v, (dict, list)):
                    self.counts["tool_value"] += 1
                    out[k] = mask_token(v, self.salt)
                else:
                    out[k] = self.value(v, request)
            return out
        if isinstance(node, list):
            return [self.value(v, request) for v in node]
        if isinstance(node, str):
            return self.text(node)
        return node


def _walk(node: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(node, Mapping):
        for k, v in node.items():
            if isinstance(v, (Mapping, list)):
                yield from _walk(v)
            else:
                yield str(k), v
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def redact_turns(turns: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]],
                 profile: Mapping[str, Any], privacy: Mapping[str, Any], salt: str) -> tuple[list[dict], list[dict], dict]:
    """(turns, tools, counts): the call's words and tool traffic with personal data removed."""
    r = Redactor(profile, privacy, salt)
    r.learn([t.get("text", "") for t in turns], [t.get("args") or {} for t in tools])
    new_turns = [{**t, "text": r.text(t.get("text", ""))} for t in turns]
    new_tools = [{**t, "args": r.value(t.get("args") or {}, request=True), "result": r.value(t.get("result"))}
                 for t in tools]
    return new_turns, new_tools, dict(r.counts)
