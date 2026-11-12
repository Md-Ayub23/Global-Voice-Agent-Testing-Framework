"""Call quality metrics (Phase 5): numbers read off a call's timeline, each with a good / warn / bad band.

Every metric here is computed from events.jsonl alone (the recording's timings, what was heard, the
tool calls), so it works for any agent and needs nothing from it. A metric informs; it blocks a
release only when the agent's owner switches it on (profile metrics.<id>.blocks) and the run's value
lands in the bad band. The p50 / p95 response-time gate (thresholds) is separate and unchanged.

  call_quality(events, mode, scenario, profile)   -> verdict.metrics.quality  {id: {value, band, ...}}
  run_quality(verdicts, profile)                   -> decision.quality         [row per metric]
  by_condition(verdicts)                           -> decision.by_condition    [row per caller condition]

Bands: for a "lower is better" metric, value <= good is good, <= warn is warn, above is bad; the
other way round for "higher is better". A metric with no good value has no band (shown only).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import stats
from .latency import percentile, turn_latencies

ESCALATION_TOOL = re.compile(r"transfer|escalat|hand_?off|human|operator|live_?agent", re.I)
def _marks() -> str:
    """Unicode combining marks (vowel signs, viramas) as a character class: they are part of a word in
    Tamil and other Indic scripts ("பதிவு"), but Python's \\w leaves them out and would split the word."""
    import sys
    import unicodedata

    ranges, start, prev = [], None, None
    for cp in range(0x300, min(sys.maxunicode, 0x2FFFF)):
        if unicodedata.category(chr(cp)).startswith("M"):
            if start is None:
                start = cp
            elif cp != prev + 1:
                ranges.append((start, prev))
                start = cp
            prev = cp
    if start is not None:
        ranges.append((start, prev))
    return "".join(f"\\u{a:04x}-\\u{b:04x}" if a != b else f"\\u{a:04x}" for a, b in ranges if b <= 0xFFFF)


# A word: letters of any script with their combining marks, digits, apostrophes ("don't").
WORD = re.compile(rf"(?:[^\W_]|[{_marks()}]|')+")


@dataclass(frozen=True)
class Metric:
    id: str
    title: str
    unit: str            # ms, %, wpm, turns, s
    better: str          # lower | higher
    good: float | None
    warn: float | None
    run: str             # how the run's value is made: p90 (over values), share, mean
    voice_only: bool
    about: str


METRICS: tuple[Metric, ...] = (
    Metric("ttfw_ms", "Time to first word", "ms", "lower", 1500, 3000, "p90", True,
           "From the moment the agent picked up to the first sound of its greeting."),
    # Over a run, p90 and p99 are banded only with enough answers (10 and 100, stats.MIN_FOR); with
    # fewer they are shown, not judged. They inform: the release limit is p50 / p95 in Pass rules.
    Metric("response_p90_ms", "Response time p90", "ms", "lower", 1500, 2500, "p90", True,
           "90th percentile of the agent's answer times (end of the caller's words to its first sound)."),
    Metric("response_p99_ms", "Response time p99", "ms", "lower", 2500, 4000, "p99", True,
           "99th percentile of the agent's answer times: the slowest answers callers hear. Needs 100 answers."),
    Metric("longest_silence_ms", "Longest silence from the agent", "ms", "lower", 3000, 6000, "p90", True,
           "The longest the caller waited for the agent in a call: before an answer, or in a pause "
           "in the middle of one (for example during a tool call)."),
    Metric("talked_over", "Agent talked over the caller", "%", "lower", 5, 15, "share", True,
           "Share of calls where the agent started talking before the caller had finished."),
    Metric("barge_in_stop_ms", "Stop time after a cut-in", "ms", "lower", 800, 1500, "p90", True,
           "How long the agent kept talking after the caller cut in (scripted cut-ins and callers who cut in)."),
    Metric("backchannel_kept", "Kept talking through \"mm-hm\"", "%", "higher", 90, 70, "share", True,
           "Share of the caller's \"mm-hm\"s the agent talked through, as it should, instead of stopping."),
    Metric("checkin_ms", "Checked in after caller silence", "ms", "lower", 7000, 12000, "p90", True,
           "When the caller went quiet, how long until the agent checked in (\"are you still there?\")."),
    Metric("talk_share", "Agent share of talk time", "%", "lower", 65, 80, "mean", True,
           "The agent's speaking time as a share of all speaking time in the call."),
    Metric("agent_wpm", "Agent speaking speed", "wpm", "lower", 190, 220, "mean", True,
           "Words per minute in the agent's turns, from what the caller heard."),
    Metric("heard_wer", "Agent words heard wrong (WER)", "%", "lower", 10, 20, "mean", True,
           "Word error rate between the words the agent reports saying and what the caller heard. Only "
           "for agents that report their own words; high values mean callers mishear the agent."),
    Metric("caller_wer", "Caller words the agent misheard (WER)", "%", "lower", 10, 20, "mean", True,
           "Word error rate between what the caller said and what the agent reports it understood "
           "(its own speech recognition). Only for agents that report it: the \"heard\" message, or a "
           "log pattern (Logs › What the agent heard)."),
    Metric("turns_to_goal", "Turns to reach the goal", "turns", "lower", None, None, "mean", False,
           "Caller turns until the test's action succeeded (the outcome's tool answered ok)."),
    Metric("escalated", "Handed to a person", "%", "lower", None, None, "share", False,
           "Share of calls the agent handed over or transferred (a handoff, or a transfer / human tool)."),
    Metric("duration_s", "Call length", "s", "lower", None, None, "mean", False, "How long the call lasted."),
)
BY_ID = {m.id: m for m in METRICS}


def settings(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each metric's good / warn / blocks, with the agent's own values (profile metrics) over the defaults."""
    own = profile.get("metrics", {}) or {}
    out = {}
    for m in METRICS:
        o = own.get(m.id, {}) or {}
        out[m.id] = {"good": o.get("good", m.good), "warn": o.get("warn", m.warn), "blocks": bool(o.get("blocks", False))}
    return out


def band(value: float | None, metric: Metric, good: float | None, warn: float | None) -> str | None:
    if value is None or good is None:
        return None
    warn = good if warn is None else warn
    if metric.better == "higher":
        return "good" if value >= good else ("warn" if value >= warn else "bad")
    return "good" if value <= good else ("warn" if value <= warn else "bad")


# ---- per call ---------------------------------------------------------------
def _ev(e: Any) -> dict[str, Any]:
    if isinstance(e, Mapping):
        return dict(e)
    return {"seq": e.seq, "t_ms": e.t_ms, "type": e.type, "data": e.data, "turn": e.turn, "source": e.source}


def _span(d: Mapping[str, Any]) -> tuple[int, int] | None:
    a = d.get("audio")
    return (a["start_ms"], a["end_ms"]) if a and a.get("end_ms", 0) >= a.get("start_ms", 0) else None


def normalise(text: str) -> list[str]:
    """Words for comparing what the agent said with what was heard, written the same way: lower case,
    no punctuation, "a.m." / "a m" as "am", numbers as digits run together ("6 0 6, 4 6 5" and
    "606465" are one token, "9:30am" is "930 am"), the way speech recognition scoring normalises."""
    t = re.sub(r"\d", lambda m: str(int(m.group(0))), text.lower())     # digits of any script: ௭ -> 7
    t = re.sub(r"\b([ap])\.?\s?m\b\.?", r"\1m", t)                 # a.m., a m, am -> am
    t = re.sub(r"(?<=\d)[:,.\-](?=\d)", "", t)                       # 9:30, 606,465 -> 930, 606465
    t = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", t)          # 30am -> 30 am
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", "", t)                       # check-up, checkup -> checkup
    words = WORD.findall(t)
    out: list[str] = []
    for w in words:                                                  # digits said one by one: one number
        if w.isdigit() and out and out[-1].isdigit():
            out[-1] += w
        else:
            out.append(w)
    return out


def wer(ref: str, hyp: str, cut: bool = False) -> tuple[int, int]:
    """(word errors, reference words): edit distance over normalised words.

    cut: the agent stopped part way (the caller cut in, or it stopped on an "mm-hm"), so the words
    after the stop were never spoken. Then the heard words are compared with the start of the
    reference that fits them best, and the unspoken rest is not counted as heard wrong.
    """
    r, h = normalise(ref), normalise(hyp)
    prev = list(range(len(h) + 1))
    best = (prev[-1], 0)                          # (errors, reference words) for the empty start
    for i, rw in enumerate(r, start=1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
        if cut and cur[-1] <= best[0]:
            best = (cur[-1], i)
    return best if cut else (prev[-1], len(r))


def _value(v: float | None, n: int | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"value": None if v is None else (round(v, 1) if isinstance(v, float) else v)}
    if n is not None:
        out["n"] = n
    out.update({k: x for k, x in extra.items() if x is not None})
    return out


def call_quality(events: Iterable[Any], mode: str, scenario: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The raw values of one call (no bands yet: bands depend on the profile, see with_bands)."""
    ev = [_ev(e) for e in events]
    scenario = scenario or {}
    agent = [e for e in ev if e["type"] == "agent_turn"]
    caller = [e for e in ev if e["type"] == "caller_turn"]
    out: dict[str, dict[str, Any]] = {}

    ended = [e for e in ev if e["type"] == "call_ended"]
    out["duration_s"] = _value(round(ended[-1]["t_ms"] / 1000, 1) if ended else None)
    out["escalated"] = _value(any(e["type"] == "handoff" or (e["type"] == "tool_request"
                                  and ESCALATION_TOOL.search(str(e["data"].get("tool", "")))) for e in ev))
    goal_tool = ((scenario.get("outcome") or {}).get("action") or {}).get("tool")
    reached = [e for e in ev if goal_tool and e["type"] == "tool_response" and e["data"].get("tool") == goal_tool
               and e["data"].get("ok")]
    if goal_tool:
        at = reached[0]["turn"] if reached and reached[0].get("turn") else None
        turns = len({c["turn"] for c in caller if at is not None and c.get("turn") and c["turn"] <= at})
        out["turns_to_goal"] = _value(turns if at is not None else None, reached=bool(reached))
    if mode != "voice":
        return out

    # Time to first word: the greeting's first sound after the agent answered.
    greet = next((e for e in agent if e["data"].get("answered_at_ms") is not None), None)
    if greet and _span(greet["data"]):
        out["ttfw_ms"] = _value(_span(greet["data"])[0] - greet["data"]["answered_at_ms"])
    elif greet:
        out["ttfw_ms"] = _value(None, note="the agent said nothing first")

    rows = turn_latencies(ev)
    timed = [r["ms"] for r in rows if r["counted"]]
    out["response_p90_ms"] = _value(percentile(timed, 90), len(timed))
    out["response_p99_ms"] = _value(percentile(timed, 99), len(timed))

    # Longest silence from the agent: an answer's wait, or a pause inside one turn that went on.
    waits = list(timed)
    last_end: dict[int, int] = {}
    for e in agent:
        s = _span(e["data"])
        if not s:
            continue
        if e["data"].get("went_on") and e.get("turn") in last_end:
            waits.append(s[0] - last_end[e["turn"]])
        last_end[e["turn"]] = s[1]
    out["longest_silence_ms"] = _value(max(waits) if waits else None)

    # Agent talked over the caller: started before the caller had finished. An answer to a caller's cut-in
    # is left out (the agent was already talking when the caller cut in).
    cut_answers, last_caller = set(), None
    for e in ev:
        if e["type"] == "caller_turn":
            last_caller = e
        elif e["type"] == "agent_turn" and last_caller is not None:
            if last_caller["data"].get("barge_in"):
                cut_answers.add(e["seq"])
            last_caller = None
    over = [r for r in rows if r["ms"] is not None and r["ms"] < 0 and r["seq"] not in cut_answers]
    out["talked_over"] = _value(len(over) > 0, len(over))

    stops = [e["data"]["stopped_after_ms"] for e in agent if e["data"].get("interrupted")
             and e["data"].get("stopped_after_ms") is not None]
    if stops:
        out["barge_in_stop_ms"] = _value(max(stops), len(stops), each=stops)

    sounds = [e for e in ev if e["type"] == "caller_sound"]
    bcs = [s for s in sounds if s["data"].get("kind") == "backchannel"]
    if bcs:
        kept = sum(bool(s["data"].get("agent_kept_talking")) for s in bcs)
        out["backchannel_kept"] = _value(round(100 * kept / len(bcs), 1), len(bcs), kept=kept)
    quiet = [s for s in sounds if s["data"].get("kind") == "silence"]
    if quiet:
        d = quiet[0]["data"]
        out["checkin_ms"] = _value(d.get("agent_spoke_after_ms"), never=d.get("agent_spoke_after_ms") is None or None,
                                   waited_ms=d.get("quiet_ms"))

    agent_ms = sum(s[1] - s[0] for e in agent if (s := _span(e["data"])))
    caller_ms = sum(s[1] - s[0] for c in caller if (s := _span(c["data"])))
    if agent_ms + caller_ms:
        out["talk_share"] = _value(round(100 * agent_ms / (agent_ms + caller_ms), 1))
    words = sum(len(WORD.findall(e["data"].get("heard_text", "").lower())) for e in agent if _span(e["data"]))
    if agent_ms >= 3000 and words:
        out["agent_wpm"] = _value(round(words / (agent_ms / 60000)))
    errs = refs = 0
    stopped_on = {s["data"].get("agent_turn") for s in bcs if not s["data"].get("agent_kept_talking")}
    for e in agent:
        own = e["data"].get("platform_transcript")
        if own:
            cut = bool(e["data"].get("interrupted")) or e.get("seq") in stopped_on
            a, b = wer(own, e["data"].get("heard_text", ""), cut=cut)
            errs, refs = errs + a, refs + b
    if refs:
        out["heard_wer"] = _value(round(100 * errs / refs, 1), refs, errors=errs)
    # What the agent understood of the caller, when it reports it: all its "heard" texts against all the
    # caller's words, each joined in order (the agent may split or join turns differently from Vocto).
    understood = [e["data"].get("text", "") for e in ev if e["type"] == "agent_heard"]
    if understood:
        # A last caller turn the agent never answered (a goodbye as the call ends) may never reach its
        # speech recogniser, so it is left out rather than counted as words the agent missed.
        last_reply = max((e.get("seq", 0) for e in agent), default=-1)
        said = " ".join(c["data"].get("text", "") for c in caller
                        if c["data"].get("text") and c.get("seq", 0) < last_reply)
        a, b = wer(said, " ".join(understood))
        if b:
            out["caller_wer"] = _value(round(100 * a / b, 1), b, errors=a)
    return out


def with_bands(quality: Mapping[str, Mapping[str, Any]], profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The call's values with each one's band (per call: a call's own p90, its own share)."""
    cfg = settings(profile)
    out = {}
    for mid, row in quality.items():
        m = BY_ID[mid]
        v = row.get("value")
        if isinstance(v, bool):
            v = 100.0 if v else 0.0          # one call: "talked over" is all or nothing
        if mid == "checkin_ms" and row.get("never"):
            v = float("inf")
        out[mid] = {**row, "band": band(v, m, cfg[mid]["good"], cfg[mid]["warn"])}
    return out


# ---- per run ----------------------------------------------------------------
def _quality(v: Mapping[str, Any]) -> Mapping[str, Any]:
    return (v.get("metrics") or {}).get("quality") or {}


def run_quality(verdicts: Sequence[Mapping[str, Any]], profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per metric seen in the run: its run value, band, and whether it blocks."""
    cfg = settings(profile)
    calls = [v for v in verdicts if v.get("status") != "ERROR"]
    rows = []
    for m in METRICS:
        vals = [(v, _quality(v)[m.id]) for v in calls if m.id in _quality(v)]
        if not vals:
            continue
        value: float | None
        detail: dict[str, Any] = {}
        if m.id in ("response_p90_ms", "response_p99_ms"):
            timed = [ms for v, _ in vals for ms in (v.get("metrics") or {}).get("latency_ms", [])]
            value = percentile(timed, 90 if m.id == "response_p90_ms" else 99)
            detail["turns"] = len(timed)
        elif m.id == "barge_in_stop_ms":
            each = [x for _, q in vals for x in q.get("each", [q["value"]]) if x is not None]
            value = percentile(each, 90)
            detail["cut_ins"] = len(each)
        elif m.id == "backchannel_kept":
            n = sum(q.get("n", 0) for _, q in vals)
            kept = sum(q.get("kept", 0) for _, q in vals)
            value = round(100 * kept / n, 1) if n else None
            detail["backchannels"] = n
        elif m.id in ("heard_wer", "caller_wer"):
            refs = sum(q.get("n", 0) for _, q in vals)
            value = round(100 * sum(q.get("errors", 0) for _, q in vals) / refs, 1) if refs else None
            detail["words"] = refs
        elif m.id == "checkin_ms":
            waits = [float("inf") if q.get("never") else q["value"] for _, q in vals]
            never = sum(1 for _, q in vals if q.get("never"))
            p = percentile([w for w in waits if w != float("inf")] + [10 ** 9] * never, 90) if waits else None
            value = None if p is None or p >= 10 ** 9 else p
            detail.update(silences=len(waits), never=never)
            if value is None and never:
                rows.append(_row(m, cfg, None, len(vals), {**detail, "note": "the agent never checked in"},
                                 forced="bad" if cfg[m.id]["good"] is not None else None))
                continue
        elif m.run == "share":
            hits = sum(bool(q["value"]) for _, q in vals)
            value = round(100 * hits / len(vals), 1)
            detail["hits"] = hits
        elif m.run == "p90":
            value = percentile([q["value"] for _, q in vals if q.get("value") is not None], 90)
        else:
            nums = [q["value"] for _, q in vals if q.get("value") is not None]
            value = round(sum(nums) / len(nums), 1) if nums else None
        # Answer-time percentiles describe a spread of many answers: of too few answers, a p90 or p99 is just
        # one of the slowest few, so it is shown, never banded (so never blocks). The other p90 metrics read
        # each call's worst moment (a 9 s silence), which is a real problem even in one call: they keep bands.
        samples = detail.get("turns", 0)
        if m.id in ("response_p90_ms", "response_p99_ms") and value is not None \
                and not stats.enough(90 if m.run == "p90" else 99, samples):
            detail = {**detail, "too_few": True, "needs": stats.need_for(90 if m.run == "p90" else 99),
                      "note": f"too few to judge ({samples} of {stats.need_for(90 if m.run == 'p90' else 99)} needed)"}
            rows.append(_row(m, cfg, value, len(vals), detail, unbanded=True))
            continue
        rows.append(_row(m, cfg, value, len(vals), detail))
    return rows


def _row(m: Metric, cfg, value, calls: int, detail: Mapping[str, Any], forced: str | None = None,
         unbanded: bool = False) -> dict[str, Any]:
    c = cfg[m.id]
    return {"id": m.id, "title": m.title, "unit": m.unit, "better": m.better, "value": value,
            "band": None if unbanded else (forced or band(value, m, c["good"], c["warn"])), "good": c["good"], "warn": c["warn"],
            "blocks": c["blocks"], "calls": calls, "about": m.about, **({"detail": dict(detail)} if detail else {})}


def by_condition(verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Results split by the caller conditions each call ran under (voice runs): does noise, a phone line
    or an accent change how often the agent passes and how fast it answers?

    A Speech mode sweep (several run conditions) groups by condition ("Phone line", "Quiet caller");
    otherwise calls group by what their line sounded like ("cafe noise 10 dB, phone line")."""
    voiced = [v for v in verdicts if v.get("conditions") and v.get("status") != "ERROR"]
    sweep = len({v["conditions"].get("preset", "as_written") for v in voiced}) > 1
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for v in voiced:
        c = v["conditions"]
        key = (c.get("preset") or "as_written") if sweep else c.get("label", "clean line")
        groups[key].append(v)

    # The clean result first, then in the order the run made them (a sweep's own order); without a
    # sweep, by name as before.
    first = list(groups)
    def order(kv):
        k = kv[0]
        return (k not in ("as_written", "clean line"), first.index(k) if sweep else 0, k)

    rows = []
    for key, vs in sorted(groups.items(), key=order):
        timed = [ms for v in vs for ms in (v.get("metrics") or {}).get("latency_ms", [])]
        passed = sum(v["status"] == "PASS" for v in vs)
        failed = sum(v["status"] == "FAIL" for v in vs)
        over = [bool(_quality(v).get("talked_over", {}).get("value")) for v in vs if "talked_over" in _quality(v)]
        # Did the caller's details reach the system as said (entity checks), and were they read back right?
        ent = [c["result"] for v in vs for c in v.get("checks", []) if c.get("type") == "entity_fidelity"
               and c["result"] in ("PASS", "FAIL")]
        rb = [c["result"] for v in vs for c in v.get("checks", []) if c.get("type") == "readback"
              and c["result"] in ("PASS", "FAIL")]
        und = [_quality(v)["caller_wer"] for v in vs if (_quality(v).get("caller_wer") or {}).get("value") is not None]
        refs = sum(u.get("n") or 0 for u in und)
        errs = sum(u.get("errors") or 0 for u in und)
        labels = sorted({v["conditions"].get("label", "clean line") for v in vs})
        rows.append({"key": key, "label": (vs[0]["conditions"].get("title") or key) if sweep else key,
                     "preset": vs[0]["conditions"].get("preset") if sweep else None,
                     "lines": labels if sweep else [],
                     "calls": len(vs), "passed": passed, "failed": failed,
                     "pass_rate": round(passed / (passed + failed), 3) if passed + failed else None,
                     "details_right": ent.count("PASS"), "details_checked": len(ent),
                     "readback_right": rb.count("PASS"), "readback_checked": len(rb),
                     "understood_wer": round(100 * errs / refs, 1) if refs else None, "understood_words": refs,
                     "failed_calls": [v["test_id"] for v in vs if v["status"] == "FAIL"],
                     "failed_tests": sorted({v["scenario_id"] for v in vs if v["status"] == "FAIL"}),
                     "p50_ms": percentile(timed, 50), "p90_ms": percentile(timed, 90),
                     "talked_over_pct": round(100 * sum(over) / len(over), 1) if over else None,
                     "behaviours": sorted({b for v in vs for b in v["conditions"].get("behaviours", [])})})
    return rows


def metric_list(profile: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """For the UI's Pass rules page: every metric with its defaults and this agent's values."""
    cfg = settings(profile or {})
    return [{"id": m.id, "title": m.title, "unit": m.unit, "better": m.better, "default_good": m.good,
             "default_warn": m.warn, "voice_only": m.voice_only, "about": m.about, **cfg[m.id]} for m in METRICS]
