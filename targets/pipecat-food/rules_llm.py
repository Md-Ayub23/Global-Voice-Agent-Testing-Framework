"""A scripted stand-in for the LLM, for a free check that the whole pipeline is wired up.

LLM_PROVIDER=rules starts this small OpenAI-compatible server inside bot.py and points Pipecat's
OpenAI-style LLM service at it. It follows the food ordering flow by keywords (pizza or sushi, size,
type, count, yes or change) and calls the same flow functions a real LLM would, so the speech
models, the Twilio stream, the Flows transitions, the tool calls, the order API and the database
are all exercised for real, at no cost.

It ignores the prompts, so it can't show the prompt-level planted bugs (skip_confirmation,
leaks_secret, rude_tone): those need a real LLM (Claude or Ollama). It is not a test of the agent's
judgement, only of the plumbing; Vocto's own tests use it that way.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SIZES = ("small", "medium", "large")
PIZZAS = ("pepperoni", "cheese", "supreme", "vegetarian")
ROLLS = ("california", "spicy tuna", "rainbow", "dragon")
NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten".split())}
YES = re.compile(r"\b(yes|yeah|yep|correct|right|that's it|sounds good|perfect|go ahead|please do)\b")
CHANGE = re.compile(r"\b(change|actually|instead|no\b|wrong)\b")


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def decide(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """{"text": ...} or {"tool": name, "args": {...}} for the next assistant turn."""
    names = {t.get("function", {}).get("name") for t in tools or []}
    # The newest caller words or function result; Flows adds the next node's instructions after a result.
    last = next((m for m in reversed(messages) if m.get("role") in ("user", "tool", "assistant")), {})
    if last.get("role") == "tool":
        try:
            result = json.loads(_text(last.get("content")))
        except ValueError:
            result = {}
        result = result if isinstance(result, dict) else {}
        call = next((c for m in reversed(messages) if m.get("role") == "assistant" for c in m.get("tool_calls") or []), {})
        name = call.get("function", {}).get("name", "")
        if name == "choose_pizza":
            return {"text": "Great, a pizza. What size would you like, small, medium or large, and pepperoni, "
                            "cheese, supreme or vegetarian?"}
        if name == "choose_sushi":
            return {"text": "Sushi it is. How many rolls, and which kind: california, spicy tuna, rainbow or dragon?"}
        if name in ("select_pizza_order", "select_sushi_order"):
            what = (f"one {result.get('size')} {result.get('type')} pizza" if name == "select_pizza_order"
                    else f"{result.get('count')} {result.get('type')} rolls")
            return {"text": f"So that's {what}, for {result.get('price')} dollars. Is that right?"}
        if name == "complete_order":
            if result.get("status") == "placed":
                number = f" Your order number is {result['order_id']}." if result.get("order_id") else ""
                return {"text": f"Thank you, your order is placed.{number} Goodbye!"}
            return {"text": "I'm sorry, our order system isn't working right now, so I couldn't place your order. "
                            "Please call back a little later."}
        if name == "get_delivery_estimate":
            if result.get("time"):
                return {"text": f"It should be with you by about {result['time']}."}
            return {"text": "Sorry, I can't get a delivery time right now."}
        if name == "revise_order":
            return {"text": "No problem. Would you like pizza or sushi?"}
        return {"text": "Okay."}
    said = next((_text(m.get("content")).lower() for m in reversed(messages) if m.get("role") == "user"), None)
    if said is None:
        return {"text": "Hi there! Thanks for calling. Would you like pizza or sushi today?"}
    if "get_delivery_estimate" in names and re.search(r"\b(how long|delivery|arrive|when will)\b", said):
        return {"tool": "get_delivery_estimate", "args": {}}
    if "choose_pizza" in names and "pizza" in said:
        return {"tool": "choose_pizza", "args": {}}
    if "choose_sushi" in names and "sushi" in said:
        return {"tool": "choose_sushi", "args": {}}
    if "select_pizza_order" in names:
        size = next((s for s in SIZES if re.search(rf"\b{s}\b", said)), None)
        kind = next((p for p in PIZZAS if re.search(r"\b" + p[:-1], said)), None)
        if size and kind:
            return {"tool": "select_pizza_order", "args": {"size": size, "pizza_type": kind}}
        return {"text": "Which size and which kind of pizza would you like?"}
    if "select_sushi_order" in names:
        m = re.search(r"\b(\d+|" + "|".join(NUMBERS) + r")\b", said)
        count = (int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]) if m else None
        kind = next((r for r in ROLLS if re.search(r"\b" + r[:-1], said)), None)   # "spicy tuner" is spicy tuna
        if count and kind:
            return {"tool": "select_sushi_order", "args": {"count": count, "roll_type": kind}}
        return {"text": "How many rolls, and which kind?"}
    if "complete_order" in names:
        if CHANGE.search(said):
            return {"tool": "revise_order", "args": {}}
        if YES.search(said):
            return {"tool": "complete_order", "args": {}}
        return {"text": "Shall I place the order?"}
    return {"text": "Would you like pizza or sushi?"}


def _chunks(answer: dict[str, Any], model: str) -> list[dict[str, Any]]:
    base = {"id": "chatcmpl-" + secrets.token_hex(6), "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model}
    if "tool" in answer:
        call = {"index": 0, "id": "call_" + secrets.token_hex(6), "type": "function",
                "function": {"name": answer["tool"], "arguments": json.dumps(answer["args"])}}
        deltas = [({"role": "assistant", "tool_calls": [call]}, None), ({}, "tool_calls")]
    else:
        words = answer["text"].split(" ")
        deltas = [({"role": "assistant", "content": ""}, None)]
        deltas += [({"content": w + (" " if i < len(words) - 1 else "")}, None) for i, w in enumerate(words)]
        deltas += [({}, "stop")]
    out = [{**base, "choices": [{"index": 0, "delta": d, "finish_reason": f}]} for d, f in deltas]
    out.append({**base, "choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        answer = decide(req.get("messages", []), req.get("tools", []))
        chunks = _chunks(answer, str(req.get("model", "rules")))
        self.send_response(200)
        if req.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True
            return
        message: dict[str, Any] = {"role": "assistant", "content": answer.get("text")}
        if "tool" in answer:
            message["tool_calls"] = chunks[0]["choices"][0]["delta"]["tool_calls"]
        body = json.dumps({"id": chunks[0]["id"], "object": "chat.completion", "created": chunks[0]["created"],
                           "model": chunks[0]["model"], "choices": [{"index": 0, "message": message,
                           "finish_reason": "tool_calls" if "tool" in answer else "stop"}]}).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 0) -> str:
    """Starts the server in a thread; returns its OpenAI-style base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="rules-llm").start()
    return f"http://127.0.0.1:{server.server_address[1]}/v1"
