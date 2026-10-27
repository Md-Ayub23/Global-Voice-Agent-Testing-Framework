"""The clinic's booking backend: the "upstream" the tool proxy forwards to.

Each handler takes the tool arguments and the request headers, and returns
(http_status, json_body), the same shape an HTTP service would. The test id
arrives in the X-Test-Id header and is stored on every appointment, so the
framework's backend probe can find exactly the rows a call created.

The database is zoo/clinic/clinic.db (override with the CLINIC_DB variable).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("CLINIC_DB", Path(__file__).resolve().parent / "clinic.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).replace(".", " ")).strip().lower()


def _doctor_id(conn: sqlite3.Connection, name: str) -> str | None:
    wanted = _norm(name)
    for row in conn.execute("SELECT id, name FROM doctors"):
        # "Dr Patel", "Dr. Patel" and "Patel" all find the same doctor.
        if _norm(row["name"]) == wanted or _norm(row["name"]).split()[-1] == wanted.split()[-1]:
            return row["id"]
    return None


def find_patient(args: dict, headers: dict) -> tuple[int, dict]:
    name, dob = args.get("full_name"), args.get("date_of_birth")
    if not name or not dob:
        return 400, {"error": "full_name and date_of_birth are required"}
    with _connect() as conn:
        for row in conn.execute("SELECT id, full_name, date_of_birth FROM patients"):
            if _norm(row["full_name"]) == _norm(name) and row["date_of_birth"] == str(dob).strip():
                return 200, {"patient_id": row["id"]}
    return 404, {"error": "no patient with that name and date of birth"}


def list_free_slots(args: dict, headers: dict) -> tuple[int, dict]:
    doctor, date = args.get("doctor"), args.get("date")
    if not doctor or not date:
        return 400, {"error": "doctor and date are required"}
    with _connect() as conn:
        doctor_id = _doctor_id(conn, doctor)
        if doctor_id is None:
            return 404, {"error": f"unknown doctor {doctor}"}
        rows = conn.execute(
            """SELECT s.slot_start FROM slots s
               WHERE s.doctor_id = ? AND substr(s.slot_start, 1, 10) = ?
                 AND NOT EXISTS (SELECT 1 FROM appointments a
                                 WHERE a.doctor_id = s.doctor_id AND a.slot_start = s.slot_start
                                   AND a.status = 'booked')
               ORDER BY s.slot_start""",
            (doctor_id, str(date)[:10]),
        ).fetchall()
    return 200, {"slots": [r["slot_start"] for r in rows]}


def book_appointment(args: dict, headers: dict) -> tuple[int, dict]:
    patient_id, doctor, slot = args.get("patient_id"), args.get("doctor"), args.get("slot_start")
    if not patient_id or not doctor or not slot:
        return 400, {"error": "patient_id, doctor and slot_start are required"}
    with _connect() as conn:
        doctor_id = _doctor_id(conn, doctor)
        if doctor_id is None:
            return 404, {"error": f"unknown doctor {doctor}"}
        if not conn.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone():
            return 404, {"error": "unknown patient"}
        if not conn.execute(
            "SELECT 1 FROM slots WHERE doctor_id = ? AND slot_start = ?", (doctor_id, slot)
        ).fetchone():
            return 409, {"error": "that slot does not exist"}
        cur = conn.execute(
            "INSERT INTO appointments (patient_id, doctor_id, slot_start, status, test_id, created_at)"
            " VALUES (?, ?, ?, 'booked', ?, ?)",
            (patient_id, doctor_id, slot, headers.get("X-Test-Id"),
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return 201, {"appointment_id": cur.lastrowid, "status": "booked"}


def cancel_appointment(args: dict, headers: dict) -> tuple[int, dict]:
    appointment_id = args.get("appointment_id")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE appointments SET status = 'cancelled' WHERE id = ? AND status = 'booked'",
            (appointment_id,),
        )
        if cur.rowcount == 0:
            return 404, {"error": "no booked appointment with that id"}
    return 200, {"appointment_id": appointment_id, "status": "cancelled"}
