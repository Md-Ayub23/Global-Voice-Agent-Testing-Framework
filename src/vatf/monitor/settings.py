"""The profile's `monitor` section with its defaults (Phase 8)."""

from __future__ import annotations

from typing import Any, Mapping

DETECTORS = ("email", "bank", "card", "id_number", "phone", "birth_date", "address", "name")   # applied in this order

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "intake": {"token": None, "listen": False, "port": 8791},
    "privacy": {"redact": list(DETECTORS), "keep_recording_links": False, "retention_days": 30,
                "patterns": []},
    "scoring": {"judges": True, "judge_share_pct": 100, "judge_max_per_day": 50, "judge_budget_usd_per_day": 5.0,
                "always_judge_flagged": True},
    "alerts": {"window_hours": 24, "min_calls": 10, "pass_rate_below": 85, "flagged_above": 25,
               "metrics_bad": True, "safety": True, "false_success": True},
}


def settings(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Every monitor setting, the agent's own values over the defaults (one level of sections)."""
    own = profile.get("monitor") or {}
    out: dict[str, Any] = {"enabled": own.get("enabled", DEFAULTS["enabled"])}
    for section in ("intake", "privacy", "scoring", "alerts"):
        mine = own.get(section) or {}
        out[section] = {k: (mine[k] if mine.get(k) is not None else v) for k, v in DEFAULTS[section].items()}
    return out
