"""Reads real calls from the formats agent platforms export, into one plain shape (Phase 8).

A RealCall is a dict:
  source         vocto | vapi | retell | transcript
  external_id    the platform's own call id (used to skip a call delivered twice), or None
  started_at     ISO time the call started (None if the export doesn't say)
  agent_version  the agent's version or release, if the export carries one (trends split by it)
  caller_number  the caller's number, only until redaction turns it into a hash
  ended_reason   why the call ended, as the platform says it
  recording_url  link to the platform's recording (dropped unless the owner keeps links)
  duration_ms    the call's length
  timed          True when every turn has start and end times (then timing metrics are measured)
  items          the call in order: {"kind": "turn", "role": "agent"|"caller", "text", "start_ms", "end_ms"}
                 and {"kind": "tool", "name", "args", "result", "ok", "at_ms", "id"}
  transferred    the platform says the call was handed to a person

Formats (detected from the content, see parse()):
  Vocto JSON   {"turns": [{"role", "text", "start_ms", "end_ms"}], "tools": [...], ...} - the documented
               format for any platform (docs/phase8-guide.md); also a list of these, or one per line (JSONL)
  Vapi         an end-of-call-report webhook, or a call object from Vapi's API (artifact.messages)
  Retell       a call_ended / call_analyzed webhook, or a call object (transcript_object, tool calls)
  Transcript   plain text, one turn per line: "Agent: ...", "Caller: ..." (optional [mm:ss] time stamps)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

AGENT_ROLES = {"agent", "assistant", "bot", "ai", "system_agent", "voice_agent"}
CALLER_ROLES = {"caller", "user", "customer", "client", "human", "patient", "guest", "contact"}
FAILED = re.compile(r"\b(error|failed|failure|exception|timed?\s?out|unavailable|not found|denied|invalid)\b", re.I)


class IntakeError(ValueError):
    """The file or request is not a call Vocto can read; the message says why."""


def _role(value: Any) -> str | None:
    r = str(value or "").strip().lower()
    if r in AGENT_ROLES:
        return "agent"
    if r in CALLER_ROLES:
        return "caller"
    return None


def _iso(value: Any) -> str | None:
    """An ISO time from an ISO string or epoch seconds / milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 10 ** 11 else value
        return datetime.fromtimestamp(secs, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ms(value: Any, seconds: bool = False) -> int | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(v * 1000)) if seconds else int(round(v))


def _args(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            v = json.loads(value)
            return dict(v) if isinstance(v, Mapping) else {"value": v}
        except ValueError:
            return {"value": value}
    return {}


def _result(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _ok(result: Any, explicit: Any = None) -> bool:
    """Whether a tool answered well: the export's own flag when it has one, else no error words in the answer."""
    if isinstance(explicit, bool):
        return explicit
    if isinstance(result, Mapping):
        for k in ("ok", "success", "successful"):
            if isinstance(result.get(k), bool):
                return result[k]
        if result.get("error"):
            return False
    return not FAILED.search(json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result)


def _finish(call: dict[str, Any]) -> dict[str, Any]:
    turns = [i for i in call["items"] if i["kind"] == "turn"]
    for i in turns:
        if i.get("start_ms") is not None and i.get("end_ms") is not None and i["end_ms"] < i["start_ms"]:
            i["end_ms"] = i["start_ms"]
    call["timed"] = bool(turns) and all(i.get("start_ms") is not None and i.get("end_ms") is not None for i in turns)
    if call["timed"]:
        # Sorted by when things happened; a tool without a time stays right after what came before it.
        last = 0
        for i in call["items"]:
            if i["kind"] == "turn":
                last = i["end_ms"]
            elif i.get("at_ms") is None:
                i["at_ms"] = last
        call["items"].sort(key=lambda i: (i["start_ms"] if i["kind"] == "turn" else i["at_ms"]))   # stable
        if not call.get("duration_ms"):
            call["duration_ms"] = max((i.get("end_ms") or 0) for i in turns)
    if not turns:
        raise IntakeError("the call has no turns: nothing the agent or the caller said")
    call.setdefault("transferred", False)
    return call


def _blank(source: str) -> dict[str, Any]:
    return {"source": source, "external_id": None, "started_at": None, "agent_version": None, "caller_number": None,
            "ended_reason": None, "recording_url": None, "duration_ms": None, "items": [], "transferred": False}


# ---- Vocto's own format ---------------------------------------------------------------------
def from_vocto(doc: Mapping[str, Any]) -> dict[str, Any]:
    call = _blank("vocto")
    call.update(external_id=str(doc["id"]) if doc.get("id") is not None else None,
                started_at=_iso(doc.get("started_at")), agent_version=_str(doc.get("agent_version")),
                caller_number=_str(doc.get("caller") or doc.get("caller_number") or doc.get("from")),
                ended_reason=_str(doc.get("ended_reason")), recording_url=_str(doc.get("recording_url")),
                duration_ms=_ms(doc.get("duration_ms")) or _ms(doc.get("duration_s"), seconds=True),
                transferred=bool(doc.get("transferred")))
    for n, t in enumerate(doc.get("turns") or []):
        if not isinstance(t, Mapping):
            raise IntakeError(f"turn {n + 1} is not an object")
        role = _role(t.get("role") or t.get("speaker"))
        if role is None:
            raise IntakeError(f"turn {n + 1}: role must be agent or caller (got {t.get('role')!r})")
        text = str(t.get("text") or t.get("content") or "").strip()
        start = _ms(t.get("start_ms")) if "start_ms" in t else _ms(t.get("start"), seconds=True)
        end = _ms(t.get("end_ms")) if "end_ms" in t else _ms(t.get("end"), seconds=True)
        call["items"].append({"kind": "turn", "role": role, "text": text, "start_ms": start, "end_ms": end})
        for tool in t.get("tools") or []:            # tools may also sit inside the turn that used them
            call["items"].append(_vocto_tool(tool))
    for tool in doc.get("tools") or []:
        call["items"].append(_vocto_tool(tool))
    return _finish(call)


def _vocto_tool(t: Mapping[str, Any]) -> dict[str, Any]:
    result = _result(t.get("result"))
    at = _ms(t.get("at_ms")) if "at_ms" in t else _ms(t.get("at"), seconds=True)
    return {"kind": "tool", "name": str(t.get("name") or t.get("tool") or "tool"), "args": _args(t.get("args")),
            "result": result, "ok": _ok(result, t.get("ok")), "at_ms": at, "id": _str(t.get("id"))}


def _str(v: Any) -> str | None:
    return None if v is None or v == "" else str(v)


# ---- Vapi -------------------------------------------------------------------------------------
def _is_vapi(doc: Mapping[str, Any]) -> bool:
    m = doc.get("message")
    if isinstance(m, Mapping) and str(m.get("type", "")).endswith("call-report"):
        return True
    msgs = (doc.get("artifact") or {}).get("messages") if isinstance(doc.get("artifact"), Mapping) else None
    msgs = msgs or doc.get("messages")
    return isinstance(msgs, list) and any(isinstance(x, Mapping) and ("secondsFromStart" in x or x.get("role") == "bot")
                                          for x in msgs)


def from_vapi(doc: Mapping[str, Any]) -> dict[str, Any]:
    m = doc.get("message") if isinstance(doc.get("message"), Mapping) else doc
    callobj = m.get("call") if isinstance(m.get("call"), Mapping) else (m if "artifact" in m or "messages" in m else {})
    artifact = m.get("artifact") or callobj.get("artifact") or {}
    msgs = artifact.get("messages") or m.get("messages") or callobj.get("messages") or []
    call = _blank("vapi")
    meta = callobj.get("metadata") or (callobj.get("assistant") or {}).get("metadata") or {}
    call.update(external_id=_str(callobj.get("id")), started_at=_iso(m.get("startedAt") or callobj.get("startedAt")
                                                                     or callobj.get("createdAt")),
                agent_version=_str(meta.get("agent_version") or meta.get("version")) if isinstance(meta, Mapping) else None,
                caller_number=_str((callobj.get("customer") or {}).get("number")),
                ended_reason=_str(m.get("endedReason") or callobj.get("endedReason")),
                recording_url=_str(artifact.get("recordingUrl") or m.get("recordingUrl") or callobj.get("recordingUrl")))
    secs = m.get("durationSeconds") or callobj.get("durationSeconds")
    call["duration_ms"] = _ms(secs, seconds=True) if secs else None
    if not call["duration_ms"] and m.get("startedAt") and m.get("endedAt"):
        a, b = _iso(m["startedAt"]), _iso(m["endedAt"])
        if a and b:
            call["duration_ms"] = int((datetime.fromisoformat(b.replace("Z", "+00:00"))
                                       - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() * 1000)
    tools: dict[str, dict[str, Any]] = {}
    for x in msgs:
        if not isinstance(x, Mapping):
            continue
        role = x.get("role")
        start = _ms(x.get("secondsFromStart"), seconds=True)
        if role in ("tool_calls", "tool_call"):
            for tc in x.get("toolCalls") or x.get("tool_calls") or []:
                fn = tc.get("function") or {}
                item = {"kind": "tool", "name": str(fn.get("name") or tc.get("name") or "tool"),
                        "args": _args(fn.get("arguments") if fn else tc.get("arguments")), "result": None, "ok": True,
                        "at_ms": start, "id": _str(tc.get("id"))}
                call["items"].append(item)
                if item["id"]:
                    tools[item["id"]] = item
        elif role in ("tool_call_result", "tool"):
            result = _result(x.get("result") if "result" in x else x.get("content"))
            item = tools.get(str(x.get("toolCallId") or x.get("tool_call_id") or ""))
            if item is None:
                item = {"kind": "tool", "name": str(x.get("name") or "tool"), "args": {}, "at_ms": start, "id": None}
                call["items"].append(item)
            item.update(result=result, ok=_ok(result))
        else:
            r = _role(role)
            if r is None:
                continue                                  # system prompt and other non-speech messages
            end = None
            if x.get("endTime") is not None and x.get("time") is not None and start is not None:
                end = start + max(0, int(x["endTime"]) - int(x["time"]))
            elif x.get("duration") is not None and start is not None:
                end = start + int(x["duration"])
            call["items"].append({"kind": "turn", "role": r, "text": str(x.get("message") or x.get("content") or "").strip(),
                                  "start_ms": start, "end_ms": end})
    reason = (call["ended_reason"] or "").lower()
    call["transferred"] = "transfer" in reason or "forward" in reason
    return _finish(call)


# ---- Retell -----------------------------------------------------------------------------------
def _is_retell(doc: Mapping[str, Any]) -> bool:
    c = doc.get("call") if isinstance(doc.get("call"), Mapping) else doc
    return isinstance(c, Mapping) and "call_id" in c and any(
        k in c for k in ("transcript_object", "transcript_with_tool_calls", "transcript"))


def from_retell(doc: Mapping[str, Any]) -> dict[str, Any]:
    c = doc["call"] if isinstance(doc.get("call"), Mapping) else doc
    call = _blank("retell")
    inbound = str(c.get("direction", "inbound")).lower() != "outbound"
    call.update(external_id=_str(c.get("call_id")), started_at=_iso(c.get("start_timestamp")),
                agent_version=_str(c.get("agent_version")),
                caller_number=_str(c.get("from_number") if inbound else c.get("to_number")),
                ended_reason=_str(c.get("disconnection_reason")), recording_url=_str(c.get("recording_url")))
    if c.get("start_timestamp") and c.get("end_timestamp"):
        call["duration_ms"] = int(c["end_timestamp"]) - int(c["start_timestamp"])
    rows = c.get("transcript_with_tool_calls") or c.get("transcript_object")
    tools: dict[str, dict[str, Any]] = {}
    if rows:
        last_end = 0
        for x in rows:
            if not isinstance(x, Mapping):
                continue
            role = x.get("role")
            if role == "tool_call_invocation":
                item = {"kind": "tool", "name": str(x.get("name") or "tool"), "args": _args(x.get("arguments")),
                        "result": None, "ok": True, "at_ms": last_end, "id": _str(x.get("tool_call_id"))}
                call["items"].append(item)
                if item["id"]:
                    tools[item["id"]] = item
            elif role == "tool_call_result":
                result = _result(x.get("content"))
                item = tools.get(str(x.get("tool_call_id") or ""))
                if item is None:
                    item = {"kind": "tool", "name": "tool", "args": {}, "at_ms": last_end, "id": None}
                    call["items"].append(item)
                item.update(result=result, ok=_ok(result, x.get("successful")))
            else:
                r = _role(role)
                if r is None:
                    continue
                words = [w for w in x.get("words") or [] if isinstance(w, Mapping)]
                start = _ms(words[0].get("start"), seconds=True) if words else None
                end = _ms(words[-1].get("end"), seconds=True) if words else None
                if end is not None:
                    last_end = end
                call["items"].append({"kind": "turn", "role": r, "text": str(x.get("content") or "").strip(),
                                      "start_ms": start, "end_ms": end})
    elif isinstance(c.get("transcript"), str):
        call["items"] = from_transcript(c["transcript"])["items"]
    call["transferred"] = "transfer" in (call["ended_reason"] or "").lower()
    return _finish(call)


# ---- plain transcript -------------------------------------------------------------------------
LINE = re.compile(r"^\s*(?:\[?(?P<ts>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?)\]?\s*[-–]?\s*)?"
                  r"(?P<who>[A-Za-z][\w .'-]{0,30}?)\s*:\s*(?P<text>.*)$")


def _clock(ts: str) -> int:
    parts = [float(p) for p in ts.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return int(secs * 1000)


def from_transcript(text: str, agent_names: Iterable[str] = ()) -> dict[str, Any]:
    """"Agent: hello" / "Caller: hi" lines; lines without a speaker continue the turn before.
    Speakers other than the usual words count as the agent when listed in agent_names."""
    names = {n.lower() for n in agent_names}
    call = _blank("transcript")
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = LINE.match(raw)
        who = m.group("who").strip().lower() if m else ""
        role = _role(who) or ("agent" if who in names else None)
        if m and role:
            start = _clock(m.group("ts")) if m.group("ts") else None
            call["items"].append({"kind": "turn", "role": role, "text": m.group("text").strip(),
                                  "start_ms": start, "end_ms": None})
        elif call["items"]:
            call["items"][-1]["text"] = (call["items"][-1]["text"] + " " + raw.strip()).strip()
        else:
            raise IntakeError("a transcript needs speaker labels, such as 'Agent: ...' and 'Caller: ...', "
                              f"at the start of each turn (first line: {raw.strip()[:60]!r})")
    return _finish(call)


# ---- any input --------------------------------------------------------------------------------
def from_doc(doc: Any) -> dict[str, Any] | None:
    """One call from one JSON document; None for a platform message that is not a finished call
    (Vapi status updates, Retell call_started)."""
    if not isinstance(doc, Mapping):
        raise IntakeError("expected a JSON object for a call")
    m = doc.get("message")
    if isinstance(m, Mapping) and m.get("type") and not str(m["type"]).endswith("call-report"):
        return None                                       # Vapi's other server messages
    if doc.get("event") in ("call_started",):
        return None                                       # Retell: the call has no transcript yet
    if _is_vapi(doc):
        return from_vapi(doc)
    if _is_retell(doc):
        return from_retell(doc)
    if isinstance(doc.get("turns"), list):
        return from_vocto(doc)
    if isinstance(doc.get("transcript"), str):
        out = from_transcript(doc["transcript"])
        out.update(external_id=_str(doc.get("id")), started_at=_iso(doc.get("started_at")),
                   agent_version=_str(doc.get("agent_version")))
        return out
    raise IntakeError("not a call Vocto can read: expected Vocto JSON (turns), a Vapi end-of-call report, "
                      "a Retell call, or a plain transcript")


def parse(content: bytes | str, name: str = "", agent_names: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Every call in one file or request body: a JSON object, a JSON list, JSON lines, or a transcript."""
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    text = text.strip()
    if not text:
        raise IntakeError("the file is empty")
    if text[0] in "[{" and not LINE.match(text.splitlines()[0]):
        try:
            doc = json.loads(text)
        except ValueError:
            docs = []
            for n, line in enumerate(text.splitlines(), start=1):
                if line.strip():
                    try:
                        docs.append(json.loads(line))
                    except ValueError as e:
                        raise IntakeError(f"line {n} is not valid JSON ({e.msg})") from None
        else:
            docs = doc if isinstance(doc, list) else (doc.get("calls") if isinstance(doc.get("calls"), list) else [doc])
        out = []
        for d in docs:
            call = from_doc(d)
            if call is not None:
                out.append(call)
        return out
    return [from_transcript(text, agent_names)]
