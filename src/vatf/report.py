"""Static HTML report (contracts/evaluation.py ReportWriter): one self-contained report.html per run."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence


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
.turn{margin:4px 0;font-size:14px}.who{color:var(--muted);display:inline-block;min-width:74px}
.ev{color:var(--muted);font-size:12px;font-family:ui-monospace,Consolas,monospace}
a{color:inherit}
"""


def _badge(status: str) -> str:
    return f'<span class="b {BADGE.get(status, "muted")}">{escape(status)}</span>'


def _transcript(events: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for e in events:
        d, seq = e["data"], f'<span class="ev">#{e["seq"]}</span>'
        if e["type"] == "caller_turn":
            rows.append(f'<div class="turn">{seq} <span class="who">Caller</span> {escape(d["text"])}</div>')
        elif e["type"] == "agent_turn":
            rows.append(f'<div class="turn">{seq} <span class="who">Agent</span> {escape(d["heard_text"])}</div>')
        elif e["type"] == "tool_request":
            rows.append(f'<div class="turn ev">#{e["seq"]} tool call {escape(d["tool"])} {escape(str(d["args"]))}</div>')
        elif e["type"] == "tool_response":
            rows.append(f'<div class="turn ev">#{e["seq"]} {escape(d["tool"])} returned {d.get("status")}'
                        f'{"" if d["ok"] else " (failed)"}</div>')
        elif e["type"] in ("fault_injected", "probe_result", "harness_event", "validator_result", "call_ended"):
            rows.append(f'<div class="turn ev">#{e["seq"]} {escape(e["type"])} {escape(str(d))}</div>')
    return "\n".join(rows)


def _call_section(v: Mapping[str, Any], call_dir: Path, run_dir: Path, warning: bool = False) -> str:
    rel = call_dir.relative_to(run_dir).as_posix()
    events = []
    events_file = call_dir / "events.jsonl"
    if events_file.exists():
        events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line]
    checks = "".join(
        f"<tr><td>{escape(c['check_id'])}</td><td>{_badge(c['result'])}</td><td>{escape(c['severity'])}</td>"
        f"<td>{escape(c['message'])}<br><span class='ev'>evidence: "
        f"{', '.join('#' + str(r['event']) for r in c['evidence']) or 'none'} ({escape(c['evidence_level'])})"
        f"</span></td></tr>" for c in v["checks"])
    outcome = ""
    if v.get("outcome"):
        o = v["outcome"]
        outcome = (f"<p><b>Outcome:</b> {escape(o['class'])} (expected {escape(o['expected'])}; said "
                   f"{escape(o['said'])}; writes ok {o['did'].get('ok_calls', 0)}/{o['did'].get('write_calls', 0)}; "
                   f"stored {escape(o['stored'])})</p>")
    note = " (harness error, rerun; findings shown as warnings only)" if warning else ""
    return (f"<details id='{escape(v['test_id'])}'><summary>{_badge(v['status'])} "
            f"<b>{escape(v['scenario_id'])}</b> repeat {v['repeat']} <span class='ev'>{escape(v['test_id'])}</span>"
            f"{escape(note)}</summary>{outcome}"
            f"<div class='table-wrap'><table><tr><th>Check</th><th>Result</th><th>Severity</th><th>Details</th></tr>"
            f"{checks}</table></div><h3 style='font-size:14px'>Timeline</h3>{_transcript(events)}"
            f"<p class='ev'><a href='{rel}/events.jsonl'>events.jsonl</a> · <a href='{rel}/verdict.json'>verdict.json</a>"
            f" · <a href='{rel}/call.json'>call.json</a></p></details>")


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
    cov = d["coverage"]
    cards.append(f"<div class='card'>Coverage<b>{cov['ratio']:.0%}</b><span class='sub'>{cov['verified_calls']} of "
                 f"{cov['planned_calls']} calls verified (need {cov['min_required']:.0%})</span></div>")
    rel_rows = "".join(
        f"<tr><td>{escape(r['scenario_id'])}</td><td>{escape(r['criticality'])}</td><td>{r['passed']}/{r['k']}</td>"
        f"<td>{r['failed']}</td><td>{r['unverified']}</td>"
        f"<td>{'yes' if r['pass_hat_k'] else ('no' if r['pass_hat_k'] is False else 'unknown')}</td></tr>"
        for r in d["reliability"])
    calls = "".join(_call_section(v, store.call_dir(v["run_id"], v["test_id"]), run_dir) for v in verdicts)
    errors = "".join(_call_section(v, store.call_dir(v["run_id"], v["test_id"]), run_dir, warning=True)
                     for v in error_attempts)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>vatf run {escape(d['run_id'])}</title><style>{CSS}</style></head>
<body><main>
<h1>{escape(d['profile_id'])}: release check</h1>
<div class="sub">Run {escape(d['run_id'])} · {escape(d['created_at'])} · text mode</div>
<div class="banner {BADGE[d['decision']]}">{escape(TITLE[d['decision']])}</div>
<ul class="reasons">{reasons}</ul>
<h2>Scorecard</h2><div class="grid">{''.join(cards)}</div>
<h2>Reliability (pass^k)</h2>
<div class="table-wrap"><table><tr><th>Scenario</th><th>Criticality</th><th>Passed</th><th>Failed</th>
<th>Unverified</th><th>All repeats passed</th></tr>{rel_rows}</table></div>
<p class="sub">Latency is not gated in text mode; it is measured from recorded audio in voice mode (Phase 2).</p>
<h2>Calls</h2>{calls}
{'<h2>Harness errors (rerun)</h2>' + errors if errors else ''}
</main></body></html>
"""
    out = run_dir / "report.html"
    out.write_text(html, encoding="utf-8")
    return out

