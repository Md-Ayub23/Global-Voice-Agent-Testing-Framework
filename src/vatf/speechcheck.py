"""Speech & accents: what the caller's details went through, read off one call.

  fact_type(name, value)            -> name | phone | date | email | address | code | amount | other
  said_in(value, text)              -> True when text holds the fact the way people say it ("14 March 1985"
                                       for 1985-03-14, "0 7 7 0 0" for 07700)
  details(scenario, events, salt)   -> one row per caller fact the agent sent to a tool: right or wrong, its type
  fair_test(scenario, events)       -> did Vocto's own careful listener hear each fact on the caller's line?
                                       A fact nobody could hear makes a wrong detail a test issue, not the agent's
  read_back(scenario, events, salt) -> were the details read back to the caller, and confirmed, before saving?

Everything here is code over the call's events. The careful listen itself (caller_heard events) is made
by the orchestrator after the call, from the caller's side of the recording: the audio exactly as the
agent received it, after noise and the phone line.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from .util import MASK_PREFIX, same_value

TYPES = ("name", "phone", "date", "email", "address", "code", "amount", "other")
TYPE_WORDS = {"name": "Names", "phone": "Phone numbers", "date": "Dates and times", "email": "Emails",
              "address": "Addresses", "code": "Codes and references", "amount": "Amounts", "other": "Other details"}
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
YES = re.compile(r"^\W*(yes|yeah|yep|yup|correct|that'?s (right|correct|it)|right|exactly|perfect|sure|spot on|"
                 r"absolutely|that'?s fine|sounds good|ஆம்|ஆமா|சரி)\b", re.I)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
ORDINAL = re.compile(r"(\d+)(st|nd|rd|th)\b")


def fact_type(name: str, value: Any) -> str:
    """What kind of detail a caller fact is, from its name first and its value second."""
    k, v = name.lower(), str(value).strip()
    if EMAIL.match(v) or "email" in k:
        return "email"
    if any(w in k for w in ("phone", "mobile", "tel", "contact_number")):
        return "phone"
    if ISO_DATE.match(v) or any(w in k for w in ("date", "dob", "birth", "day", "time", "when")):
        return "date"
    if any(w in k for w in ("address", "street", "postcode", "zip", "city")):
        return "address"
    if any(w in k for w in ("amount", "price", "total", "cost", "fee")) or re.match(r"^[£$€₹]\s?\d", v):
        return "amount"
    if any(w in k for w in ("name", "doctor", "surname", "first", "last")):
        return "name"
    if any(w in k for w in ("code", "ref", "reference", "number", "id", "account", "pin", "order", "policy")):
        return "phone" if len(re.sub(r"\D", "", v)) >= 9 and not re.search(r"[a-z]", v, re.I) else "code"
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 9 and len(digits) >= len(v.replace(" ", "")) - 2:
        return "phone"
    return "other"


def _words(text: str) -> list[str]:
    from .metrics import normalise

    t = ORDINAL.sub(r"\1", str(text).lower())                     # 14th -> 14
    return normalise(t)


def _numbers(text: str) -> list[str]:
    from .checks import spoken_numbers

    t = ORDINAL.sub(r"\1", str(text).lower())
    # "oh seven seven..." at the start of a number: "oh" is a zero (spoken_numbers trusts it only between digits)
    t = re.sub(r"\b(oh|o)\b(?=[\s,]+(zero|one|two|three|four|five|six|seven|eight|nine|oh|\d))", "zero", t)
    return spoken_numbers(t)


def key_parts(value: Any) -> list[str]:
    """The parts of a fact a listener must hear: a date's day, month and year; a number's digits; a name's
    words. Matching is on these, so "14 March 1985" holds 1985-03-14."""
    v = str(value).strip()
    if (m := ISO_DATE.match(v)):
        y, mo, d = m.groups()
        return [str(int(d)), MONTHS[int(mo) - 1], y]
    digits = re.sub(r"\D", "", v)
    if digits and len(digits) >= 4 and not re.search(r"[a-z]", v, re.I):
        return [digits]
    if EMAIL.match(v):
        local, domain = v.lower().split("@", 1)
        return [w for w in re.split(r"[^a-z0-9]+", local + " " + domain.rsplit(".", 1)[0]) if w]
    return _words(v)


def said_in(value: Any, text: str) -> bool:
    """Does the text hold the fact the way a person says it?"""
    parts = key_parts(value)
    if not parts or not text:
        return False
    words, nums = _words(text), _numbers(text)
    joined = "".join(nums)
    for p in parts:
        if p.isdigit():
            if len(p) <= 2:
                if p not in words and str(int(p)) not in words:
                    return False
            elif not (p in nums or p in joined or p in words):
                return False
        elif p not in words:
            return False
    return True


def _of(events: Iterable[Any], kind: str) -> list[Any]:
    return [e for e in events if (e["type"] if isinstance(e, Mapping) else e.type) == kind]


def _d(e: Any) -> Mapping[str, Any]:
    return e["data"] if isinstance(e, Mapping) else e.data


def _seq(e: Any) -> int:
    return e["seq"] if isinstance(e, Mapping) else e.seq


def _facts(scenario: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ((scenario.get("caller") or {}).get("facts") or {}).items()
            if v is not None and str(v).strip() and not isinstance(v, (dict, list))}


def details(scenario: Mapping[str, Any], events: Sequence[Any], salt: str) -> list[dict[str, Any]]:
    """Each caller fact the agent passed to a tool: did it arrive as the caller said it? (One row per fact,
    wrong if any tool call carried it changed.) Facts no tool received are left out: the test may not need them."""
    facts = _facts(scenario)
    rows = []
    for name, want in facts.items():
        sent = [e for e in _of(events, "tool_request") if name in (_d(e).get("args") or {})]
        if not sent:
            continue
        wrong = [e for e in sent if not same_value(_d(e)["args"][name], want, salt)]
        got = _d(wrong[0])["args"][name] if wrong else None
        rows.append({"fact": name, "type": fact_type(name, want), "right": not wrong,
                     "said": None if str(want).startswith(MASK_PREFIX) else str(want),
                     "saved": None if got is None or str(got).startswith(MASK_PREFIX) else str(got),
                     "seq": _seq(wrong[0] if wrong else sent[0])})
    return rows


def fair_test(scenario: Mapping[str, Any], events: Sequence[Any]) -> dict[str, Any] | None:
    """Per caller fact said on the line: did Vocto's own careful listener (caller_heard events, the caller's
    audio as the agent received it) hear it? None when there was no careful listen."""
    heard = {_d(e).get("caller_turn"): _d(e).get("text", "") for e in _of(events, "caller_heard")}
    if not heard:
        return None
    facts = _facts(scenario)
    said: dict[str, bool] = {}
    for c in _of(events, "caller_turn"):
        if _seq(c) not in heard:
            continue
        text = _d(c).get("spoken") or _d(c).get("text", "")
        typed = " ".join(re.findall(r"\[keypad (\d+)\]", text))      # touch tones: not for a listener to hear
        text = re.sub(r"\[keypad \d+\]", " ", text)
        for name, value in facts.items():
            if typed and said_in(value, typed):
                continue
            if said_in(value, text):
                said[name] = said.get(name, False) or said_in(value, heard[_seq(c)])
    return {"heard": sorted(k for k, v in said.items() if v), "missed": sorted(k for k, v in said.items() if not v)}


def read_back(scenario: Mapping[str, Any], events: Sequence[Any], salt: str) -> dict[str, Any] | None:
    """Before the outcome's write tool first ran: did the agent read the caller's details back, and did the
    caller say yes? Only details the write carried count. None when the call never saved any."""
    tool = ((scenario.get("outcome") or {}).get("action") or {}).get("tool")
    facts = _facts(scenario)
    if not tool or not facts:
        return None
    write = next((e for e in _of(events, "tool_request") if _d(e).get("tool") == tool), None)
    if write is None:
        return None
    args = _d(write).get("args") or {}
    saved = [n for n in facts if n in args and same_value(args[n], facts[n], salt)
             and not str(facts[n]).startswith(MASK_PREFIX) and fact_type(n, facts[n]) != "other"]
    if not saved:
        return None
    before = [e for e in events if _seq(e) < _seq(write)
              and (e["type"] if isinstance(e, Mapping) else e.type) in ("agent_turn", "caller_turn")]
    read, confirmed = set(), False
    for i, e in enumerate(before):
        if (e["type"] if isinstance(e, Mapping) else e.type) != "agent_turn":
            continue
        d = _d(e)
        said = " ".join(x for x in (d.get("platform_transcript"), d.get("heard_text")) if x)
        hits = {n for n in saved if said_in(facts[n], said)}
        if not hits:
            continue
        read |= hits
        reply = next((c for c in before[i + 1:] if (c["type"] if isinstance(c, Mapping) else c.type) == "caller_turn"), None)
        if reply is not None and YES.search(_d(reply).get("text", "")):
            confirmed = True
    return {"details": len(saved), "read_back": len(read), "confirmed": confirmed,
            "ok": len(read) == len(saved) and confirmed, "missing": sorted(set(saved) - read)}


def summary(scenario: Mapping[str, Any], events: Sequence[Any], salt: str) -> dict[str, Any] | None:
    """verdict.speech: the details, the fair-test listen and the read-back of one call."""
    rows, fair, rb = details(scenario, events, salt), fair_test(scenario, events), read_back(scenario, events, salt)
    if not rows and fair is None and rb is None:
        return None
    out: dict[str, Any] = {"details": rows}
    if fair is not None:
        out["fair"] = fair
        wrong = {r["fact"] for r in rows if not r["right"]}
        # A detail saved wrong that Vocto's own listener couldn't hear either: no listener could have got it
        # right, so the line was too hard (a test issue), not the agent.
        out["too_hard"] = sorted(wrong & set(fair["missed"]))
    if rb is not None:
        out["read_back"] = rb
    return out


def voices(events: Sequence[Any]) -> str | None:
    """synthetic, real (every line played from a recording of a real person) or mixed; None without voice."""
    lines = [c for c in _of(events, "caller_turn") if _d(c).get("audio")]
    if not lines:
        return None
    real = sum(bool(_d(c).get("recording")) for c in lines)
    return "real" if real == len(lines) else "synthetic" if not real else "mixed"


def run_summary(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """decision.speech: the caller's details over the run, by type (with a likely range), read back before
    saving, details too hard to hear, the voices used, and examples of details saved wrong."""
    from . import stats

    calls = [v for v in verdicts if v.get("status") != "ERROR" and v.get("speech")]
    if not calls:
        return None
    by_type: dict[str, dict[str, int]] = {}
    wrong, too_hard, rb_ok, rb_n, kinds = [], 0, 0, 0, set()
    for v in calls:
        s = v["speech"]
        hard = set(s.get("too_hard") or [])
        too_hard += bool(hard)
        if s.get("voices"):
            kinds.add(s["voices"])
        for r in s.get("details") or []:
            if r["fact"] in hard:
                continue                            # a test issue: counted apart, not as the agent's
            t = by_type.setdefault(r["type"], {"right": 0, "checked": 0})
            t["checked"] += 1
            t["right"] += bool(r["right"])
            if not r["right"]:
                wrong.append({"test_id": v["test_id"], "scenario_id": v.get("scenario_id"), "fact": r["fact"],
                              "type": r["type"], "said": r.get("said"), "saved": r.get("saved"), "seq": r.get("seq"),
                              "condition": (v.get("conditions") or {}).get("title")})
        if s.get("read_back"):
            rb_n += 1
            rb_ok += bool(s["read_back"]["ok"])
    right = sum(t["right"] for t in by_type.values())
    checked = sum(t["checked"] for t in by_type.values())
    return {
        "details": stats.rate(right, checked),
        "by_type": [{"type": k, "title": TYPE_WORDS[k], **v, **({"rate": stats.rate(v["right"], v["checked"])})}
                    for k, v in sorted(by_type.items(), key=lambda kv: TYPES.index(kv[0]))],
        "read_back": stats.rate(rb_ok, rb_n),
        "too_hard_calls": too_hard,
        "voices": "mixed" if len(kinds) > 1 else (next(iter(kinds)) if kinds else None),
        "wrong": wrong[:10],
    }
