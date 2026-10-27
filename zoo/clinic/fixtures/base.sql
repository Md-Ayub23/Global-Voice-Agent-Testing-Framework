-- Riverside Clinic test data. Loaded before every call, so each call starts clean.
-- "Today" in the scenarios is Monday 2 November 2026.

DROP TABLE IF EXISTS appointments;
DROP TABLE IF EXISTS slots;
DROP TABLE IF EXISTS doctors;
DROP TABLE IF EXISTS patients;

CREATE TABLE patients (
  id            TEXT PRIMARY KEY,
  full_name     TEXT NOT NULL,
  date_of_birth TEXT NOT NULL
);

CREATE TABLE doctors (
  id   TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE slots (
  doctor_id  TEXT NOT NULL REFERENCES doctors(id),
  slot_start TEXT NOT NULL
);

-- No unique constraint on (doctor_id, slot_start) on purpose: like many real
-- backends, it trusts the caller, so a double booking really lands in the table.
CREATE TABLE appointments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  doctor_id  TEXT NOT NULL REFERENCES doctors(id),
  slot_start TEXT NOT NULL,
  status     TEXT NOT NULL,
  test_id    TEXT,
  created_at TEXT NOT NULL
);

INSERT INTO patients VALUES
  ('P-1042', 'Maria Lopez', '1985-03-14'),
  ('P-1043', 'James Okoro', '1972-11-02'),
  ('P-1044', 'Aisha Khan',  '1990-07-21');

INSERT INTO doctors VALUES
  ('D-1', 'Dr Patel'),
  ('D-2', 'Dr Chen');

INSERT INTO slots VALUES
  ('D-1', '2026-11-03T09:30:00'),
  ('D-1', '2026-11-03T11:00:00'),
  ('D-1', '2026-11-03T14:00:00'),
  ('D-1', '2026-11-03T15:30:00'),
  ('D-1', '2026-11-04T10:00:00'),
  ('D-1', '2026-11-05T09:00:00'),
  ('D-2', '2026-11-03T10:00:00'),
  ('D-2', '2026-11-03T13:30:00'),
  ('D-2', '2026-11-04T09:30:00');
