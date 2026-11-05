"""Phase 8: monitoring real production calls.

A real call passes through these modules, one way:

  intake.py   any call export (Vocto JSON, Vapi, Retell, a plain transcript) -> one RealCall
  redact.py   personal data removed from the RealCall, BEFORE anything is written to disk
  store.py    runs/monitor/<agent>/calls/<call id>/{call.json, events.jsonl, verdict.json} + index.jsonl
  score.py    Phase 5 metrics + real-call checks + the agent's AI judges (on a capped sample) -> verdict.json
  trends.py   daily rollup, this week vs last, split by agent version
  watch.py    alert rules over a rolling window; raised / cleared alerts go out through alerts.py

settings.py reads the profile's `monitor` section (all of it editable on the dashboard's Real calls page).
"""
