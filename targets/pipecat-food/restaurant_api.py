"""The restaurant's order system: what the food ordering agent's tools talk to.

Pipecat's upstream example keeps the order in memory and never sends it anywhere. A real restaurant
agent hands the order to the restaurant's systems, so this small HTTP API stands in for them, with
a SQLite database Vocto can read after each call:

  POST /tools/place_order          {"order": {...}}  -> {"order_id", "status": "placed", "total"}
  POST /tools/delivery_estimate    {"now": ...}      -> {"minutes", "time"}
  GET  /orders?test_id=...                           -> the orders placed during one test call
  GET  /health

The X-Test-Id header (added by Vocto's tool gateway) is stored with each order, so Vocto can find
the orders of one call. Started by bot.py on port 7870 (python restaurant_api.py to run it alone).
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "orders.db"
PORT = 7870

PIZZA_PRICES = {"small": 10.00, "medium": 15.00, "large": 20.00}
PIZZA_TYPES = {"pepperoni", "cheese", "supreme", "vegetarian"}
SUSHI_TYPES = {"california", "spicy tuna", "rainbow", "dragon"}
SUSHI_PRICE = 8.00

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
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
)
"""

log = logging.getLogger("restaurant-api")
_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def place_order(args: dict[str, Any], test_id: str | None) -> tuple[int, dict[str, Any]]:
    order = args.get("order") if isinstance(args.get("order"), dict) else args
    food = str(order.get("type", "")).lower()
    if food == "pizza":
        size, kind = str(order.get("size", "")).lower(), str(order.get("pizza_type", "")).lower()
        if size not in PIZZA_PRICES or kind not in PIZZA_TYPES:
            return 422, {"error": f"we don't make a {size} {kind} pizza"}
        row = {"food": "pizza", "size": size, "pizza_type": kind, "count": 1, "roll_type": None,
               "price": PIZZA_PRICES[size]}
    elif food == "sushi":
        try:
            count = int(order.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        kind = str(order.get("roll_type", "")).lower()
        if not 1 <= count <= 10 or kind not in SUSHI_TYPES:
            return 422, {"error": f"we can't make {count} {kind} rolls"}
        row = {"food": "sushi", "size": None, "pizza_type": None, "count": count, "roll_type": kind,
               "price": count * SUSHI_PRICE}
    else:
        return 422, {"error": "an order is a pizza or sushi"}
    order_id = f"ORD-{secrets.randbelow(9000) + 1000}"
    with _lock, connect() as conn:
        conn.execute("INSERT INTO orders (id, test_id, food, size, pizza_type, count, roll_type, price, status, created_at)"
                     " VALUES (:id, :test_id, :food, :size, :pizza_type, :count, :roll_type, :price, 'placed', :at)",
                     {**row, "id": order_id, "test_id": test_id,
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return 200, {"order_id": order_id, "status": "placed", "total": row["price"]}


def delivery_estimate(args: dict[str, Any], test_id: str | None) -> tuple[int, dict[str, Any]]:
    try:
        now = datetime.fromisoformat(str(args["now"])) if args.get("now") else datetime.now()
    except ValueError:
        now = datetime.now()
    when = now + timedelta(minutes=30)
    return 200, {"minutes": 30, "time": when.strftime("%H:%M")}


TOOLS = {"place_order": place_order, "delivery_estimate": delivery_estimate}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: Any) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            args = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "the body must be JSON"})
        name = urlparse(self.path).path.removeprefix("/tools/")
        if name not in TOOLS:
            return self._send(404, {"error": f"no tool {name}"})
        test_id = self.headers.get("X-Test-Id")
        status, body = TOOLS[name](args if isinstance(args, dict) else {}, test_id)
        log.info("api %s %s -> %s", test_id or "-", name, status)
        self._send(status, body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/orders":
            test_id = parse_qs(url.query).get("test_id", [""])[0]
            with connect() as conn:
                rows = conn.execute("SELECT * FROM orders WHERE test_id = ? ORDER BY created_at", (test_id,)).fetchall()
            return self._send(200, [dict(r) for r in rows])
        self._send(200, {"service": "Pipecat food ordering: restaurant order API", "ok": True})


def serve(port: int = PORT) -> ThreadingHTTPServer:
    connect().close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="restaurant-api").start()
    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve()
    print(f"restaurant order API: http://127.0.0.1:{PORT}/tools/<name>   database {DB_PATH}")
    threading.Event().wait()
