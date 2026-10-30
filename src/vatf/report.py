"""Static HTML report (contracts/evaluation.py ReportWriter): one self-contained report.html per run."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence

from .story import database_rows, judgement, load_events, story_rows


BADGE = {"PASS": "ok", "GO": "ok", "FAIL": "bad", "NO_GO": "bad", "ERROR": "warn",
         "NOT_VERIFIED": "warn", "NEEDS_REVIEW": "warn", "NO_GO_INSUFFICIENT_EVIDENCE": "warn",
         "NOT_APPLICABLE": "muted"}
TITLE = {"GO": "GO", "NO_GO": "NO-GO", "NO_GO_INSUFFICIENT_EVIDENCE": "NO-GO: not enough evidence"}

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--card:#fff;
--ok:#1f7a3f;--ok-bg:#e5f4ea;--bad:#b42318;--bad-bg:#fdecea;--warn:#8a5a00;--warn-bg:#fdf3dc}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececee;--muted:#9a9aa0;--line:#2c2c30;
--card:#1e1e21;--ok:#5fd08a;--ok-bg:#16301f;--bad:#ff8a80;--bad-bg:#3a1a18;--warn:#f0c060;--warn-bg:#3a2e12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--muted);font-size:13px}
.banner{border-radius:10px;padding:18px 20px;margin:18px 0;font-size:24px;font-weight:650}
.banner.ok{background:var(--ok-bg);color:var(--ok)}.banner.bad{background:var(--bad-bg);color:var(--bad)}
.banner.warn{background:var(--warn-bg);color:var(--warn)}
ul.reasons{margin:0;padding-left:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.card b{font-size:22px;display:block}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.b{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.b.ok{background:var(--ok-bg);color:var(--ok)}.b.bad{background:var(--bad-bg);color:var(--bad)}
.b.warn{background:var(--warn-bg);color:var(--warn)}.b.muted{color:var(--muted);border:1px solid var(--line)}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:8px 12px}
summary{cursor:pointer}
h3{font-size:14px;margin:16px 0 6px}
table.story td.who{white-space:nowrap;font-weight:600}
tr.agent td.who{color:var(--ok)}tr.caller td.who{color:#2f6fbf}
tr.sys td,tr.db td{color:var(--muted);font-size:13px}tr.fault td{color:var(--bad)}
tr.db td{font-weight:600}
.ev{color:var(--muted);font-size:12px;font-family:ui-monospace,Consolas,monospace}
a{color:inherit}
"""


def _badge(status: str) -> str:
    return f'<span class="b {BADGE.get(status, "muted")}">{escape(status)}</span>'


WHO_CLASS = {"AGENT": "agent", "second listen": "sys", "CALLER": "caller", "vatf breaks tool": "fault", "vatf error": "fault",
             "database check": "db"}


def _story_table(events: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(
        f"<tr class='{WHO_CLASS.get(who, 'sys')}'><td class='ev'>#{n}</td><td class='who'>{escape(who)}</td>"
        f"<td>{escape(text)}</td></tr>" for n, who, text in story_rows(events))
    return (f"<div class='table-wrap'><table class='story'><tr><th>#</th><th>Who</th><th>What happened</th></tr>"
            f"{rows}</table></div>")


def _database_tables(events: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for probe, rows in database_rows(events):
        if rows:
            cols = list(rows[0])
            head = "".join(f"<th>{escape(c)}</th>" for c in cols)
            body = "".join("<tr>" + "".join(f"<td>{escape(str(r.get(c)))}</td>" for c in cols) + "</tr>"
                           for r in rows)
            table = f"<div class='table-wrap'><table><tr>{head}</tr>{body}</table></div>"
        else:
            table = "<p class='sub'>No rows: nothing was stored for this call.</p>"
        parts.append(f"<h3>Database after the call <span class='ev'>{escape(probe)}</span></h3>{table}")
    return "".join(parts)


def _voice(v: Mapping[str, Any], events: Sequence[Mapping[str, Any]], rel: str) -> str:
    """Voice calls: the recording (playable) and each answer's response time."""
    recording = next((e["data"]["recording"] for e in events if e["type"] == "call_ended"
                      and "recording" in e["data"]), None)
    rows = v.get("metrics", {}).get("latency_turns")
    if recording is None and rows is None:
        return ""
    out = ""
    if recording:
        src = f"{rel}/{recording['file']}"
        out += (f"<h3>Recording</h3><audio controls preload='none' src='{escape(src)}'></audio>"
                f"<p class='sub'>Left channel: caller. Right channel: agent, as the caller heard it. "
                f"Seconds in the file are seconds on the timeline. <a href='{escape(src)}'>call.wav</a></p>")
    if rows:
        body = "".join(
            f"<tr><td>{r['turn']}</td><td class='ev'>#{r['seq']}</td>"
            f"<td>{'-' if r['ms'] is None else str(r['ms']) + ' ms'}</td>"
            f"<td>{'counts' if r['counted'] else escape('not counted: ' + r['note'])}</td></tr>" for r in rows)
        out += ("<h3>Response times</h3><p class='sub'>End of the caller's words to the first sound of the "
                "agent's answer, read off the recording.</p><div class='table-wrap'><table><tr><th>Turn</th>"
                f"<th>Event</th><th>Answered after</th><th>Gate</th></tr>{body}</table></div>")
    return out


def _judgement(v: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> str:
    lines = judgement(v, events)
    if not lines:
        return ""
    rows = "".join(f"<tr><th>{escape(label)}</th><td>{escape(text)}</td></tr>" for label, text in lines)
    return f"<h3>How it was judged: said vs did vs stored</h3><div class='table-wrap'><table>{rows}</table></div>"


def _judges(v: Mapping[str, Any]) -> str:
    """The AI judges' results: both votes and their reasons. Advisory items cannot block yet."""
    judged = [c for c in v["checks"] if c.get("origin") == "judge"]
    if not judged:
        return ""
    rows = []
    for c in judged:
        j = c.get("judge") or {}
        votes = "<br>".join(f"<span class='ev'>{escape(x['judge'])} ({escape(x.get('model', ''))}): "
                            f"{escape(x['result'])}</span> {escape(x.get('reason', ''))}" for x in j.get("votes", []))
        how = "labelled by a person" if j.get("human") else ("can block" if c.get("can_block") else "advisory")
        rows.append(f"<tr><td>{escape(j.get('title', c['check_id']))}<br><span class='ev'>{escape(c['check_id'])}</span>"
                    f"</td><td>{_badge(c['result'])}</td><td>{escape(c['severity'])}<br><span class='ev'>{how}</span></td>"
                    f"<td>{escape(c['message'])}{('<br>' + votes) if votes else ''}</td></tr>")
    notes = "".join(f"<p class='sub'>{escape(n)}</p>" for n in v.get("judge_notes", []))
    return ("<h3>AI judges</h3><p class='sub'>Two models score the call. Advisory items are not calibrated against "
            "a person's labels yet, so they cannot block the release.</p><div class='table-wrap'><table><tr>"
            f"<th>Item</th><th>Result</th><th>Severity</th><th>Details</th></tr>{''.join(rows)}</table></div>{notes}")


def _call_section(v: Mapping[str, Any], call_dir: Path, run_dir: Path, warning: bool = False) -> str:
    rel = call_dir.relative_to(run_dir).as_posix()
    events = load_events(call_dir)
    checks = "".join(
        f"<tr><td>{escape(c['check_id'])}</td><td>{_badge(c['result'])}</td><td>{escape(c['severity'])}</td>"
        f"<td>{escape(c['message'])}<br><span class='ev'>evidence: "
        f"{', '.join('#' + str(r['event']) for r in c['evidence']) or 'none'} ({escape(c['evidence_level'])})"
        f"</span></td></tr>" for c in v["checks"])
    note = " (harness error, rerun; findings shown as warnings only)" if warning else ""
    headline = next((c["message"] for c in v["checks"] if c["check_id"] == "outcome"), "")
    opened = "" if v["status"] == "PASS" or warning else " open"
    return (f"<details id='{escape(v['test_id'])}'{opened}><summary>{_badge(v['status'])} "
            f"<b>{escape(v['scenario_id'])}</b> repeat {v['repeat']} <span class='ev'>{escape(v['test_id'])}</span>"
            f"{escape(note)}<br><span class='sub'>{escape(headline)}</span></summary>"
            f"<h3>Conversation and actions</h3>{_story_table(events)}"
            f"{_voice(v, events, rel)}{_database_tables(events)}{_judgement(v, events)}"
            f"<h3>Checks</h3><div class='table-wrap'><table><tr><th>Check</th><th>Result</th><th>Severity</th>"
            f"<th>Details</th></tr>{checks}</table></div>{_judges(v)}"
            f"<p class='ev'><a href='{rel}/story.txt'>story.txt</a> · <a href='{rel}/events.jsonl'>events.jsonl</a>"
            f" · <a href='{rel}/verdict.json'>verdict.json</a> · <a href='{rel}/call.json'>call.json</a></p></details>")


def _safety(safety: Mapping[str, Any] | None) -> str:
    """The built-in safety tests: each kind of attack, then every finding with a link to its call."""
    if not safety:
        return ""
    packs = " · ".join(f"{escape(k)} {p['calls'] - p['failed'] - p.get('to_review', 0)}/{p['calls']}"
                       for k, p in safety["packs"].items())
    rows = "".join(
        f"<tr><td>{_badge(f['result'])}</td><td><a href='#{escape(f['test_id'])}'>{escape(f['title'])}</a></td>"
        f"<td>{'advisory' if not f['can_block'] else escape(f['severity'])}</td><td>{escape(f['message'])}</td></tr>"
        for f in safety["findings"])
    table = (f"<div class='table-wrap'><table><tr><th>Result</th><th>Attack</th><th>Weight</th><th>Finding</th></tr>"
             f"{rows}</table></div>") if rows else "<p>No findings: the agent held up against every attack.</p>"
    review = f", {safety['to_review']} wait for your label" if safety.get("to_review") else ""
    return (f"<h2>Safety tests</h2><p class='sub'>{safety['passed']} of {safety['calls']} attack call(s) handled "
            f"safely, {safety['failed']} with a finding{review}. {packs}</p>{table}")


def write_report(decision: Mapping[str, Any], verdicts: Sequence[Mapping[str, Any]],
                 error_attempts: Sequence[Mapping[str, Any]], run_dir: Path, store: Any) -> Path:
    d = decision
    reasons = "".join(
        f"<li>{escape(r['message'])}" + (
            " <span class='ev'>" + ", ".join(f"<a href='#{escape(ref.split('/')[0])}'>{escape(ref)}</a>"
                                             for ref in r.get("refs", [])) + "</span>" if r.get("refs") else "")
        + "</li>" for r in d["reasons"])
    cards = []
    for name, s in d["scorecard"].items():
        score = "n/a" if s["score"] is None else f"{s['score']:.0%}"
        cards.append(f"<div class='card'>{escape(name.title())}<b>{score}</b>"
                     f"<span class='sub'>{s['passed']} passed, {s['failed']} failed, coverage {s['coverage']:.0%}</span></div>")
    lat = d.get("latency", {})
    voice = d.get("_mode") == "voice"
    if voice:
        within = lat.get("within_limits")
        shown = "n/a" if lat.get("p95_ms") is None else f"{lat['p95_ms']} ms"
        cards.append(f"<div class='card'>Response time (p95)<b>{shown}</b><span class='sub'>p50 {lat.get('p50_ms')} ms;"
                     f" limits {lat.get('p50_limit_ms')} / {lat.get('p95_limit_ms')} ms; "
                     f"{lat.get('turns_measured', 0)} answers timed"
                     f"{'' if within is None else (', within limits' if within else ', TOO SLOW')}</span></div>")
    cov = d["coverage"]
    cards.append(f"<div class='card'>Coverage<b>{cov['ratio']:.0%}</b><span class='sub'>{cov['verified_calls']} of "
                 f"{cov['planned_calls']} calls verified (need {cov['min_required']:.0%})</span></div>")
    rel_rows = "".join(
        f"<tr><td>{escape(r['scenario_id'])}</td><td>{escape(r['criticality'])}</td><td>{r['passed']}/{r['k']}</td>"
        f"<td>{r['failed']}</td><td>{r['unverified']}</td>"
        f"<td>{'yes' if r['pass_hat_k'] else ('no' if r['pass_hat_k'] is False else 'unknown')}</td></tr>"
        for r in d["reliability"])
    safety = _safety(d.get("safety"))
    calls = "".join(_call_section(v, store.call_dir(v["run_id"], v["test_id"]), run_dir) for v in verdicts)
    errors = "".join(_call_section(v, store.call_dir(v["run_id"], v["test_id"]), run_dir, warning=True)
                     for v in error_attempts)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>vatf run {escape(d['run_id'])}</title><style>{CSS}</style></head>
<body><main>
<h1>{escape(d['profile_id'])}: release check</h1>
<div class="sub">Run {escape(d['run_id'])} · {escape(d['created_at'])} · {'voice' if voice else 'text'} mode</div>
{'' if d.get('_pii_masked', True) else '<p class="b warn">Personal data is NOT masked in this run (--show-pii). Do not share or commit it.</p>'}
<div class="banner {BADGE[d['decision']]}">{escape(TITLE[d['decision']])}</div>
<ul class="reasons">{reasons}</ul>
<h2>Scorecard</h2><div class="grid">{''.join(cards)}</div>
{safety}
<h2>Reliability (pass^k)</h2>
<div class="table-wrap"><table><tr><th>Scenario</th><th>Criticality</th><th>Passed</th><th>Failed</th>
<th>Unverified</th><th>All repeats passed</th></tr>{rel_rows}</table></div>
{'<p class="sub">Response time is measured on the recorded audio of every call and gates the release.</p>' if voice else '<p class="sub">Latency is not gated in text mode; it is measured from recorded audio in voice mode.</p>'}
<h2>Calls</h2>
<p class="sub">CALLER is the simulated customer, AGENT is the voice agent under test. Tool calls, injected faults
and the database check are recorded by vatf. Failed calls are opened; click a call to open or close it.
The same text is in transcript.txt next to this file.</p>{calls}
{'<h2>Harness errors (rerun)</h2>' + errors if errors else ''}
</main></body></html>
"""
    out = run_dir / "report.html"
    out.write_text(html, encoding="utf-8")
    return out

