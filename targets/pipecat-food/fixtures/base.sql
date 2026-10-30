-- The restaurant's orders table, emptied before every test call so each call starts clean.
DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
  id          TEXT PRIMARY KEY,
  test_id     TEXT,
  food        TEXT NOT NULL,
  size        TEXT,
  pizza_type  TEXT,
  count       INTEGER,
  roll_type   TEXT,
  price       REAL NOT NULL,
  status      TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
