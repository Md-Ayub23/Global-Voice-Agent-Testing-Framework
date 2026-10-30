# Pipecat food ordering, as a phone agent for Vocto to test

Pipecat's food ordering example (Pipecat Flows, BSD 2-clause, see `LICENSE-pipecat-flows`) run as a
phone agent on this computer: a FastAPI server that takes Twilio Media Streams, with local speech
models and a real order API and database. Vocto's project for it is `examples/pipecat-food`; the
guide is `docs/real-agents-guide.md`.

This folder is its own uv project (Pipecat 1.15.0), separate from Vocto's environment.

```powershell
uv run --project targets/pipecat-food python targets/pipecat-food/bot.py                    # the agent
uv run --project targets/pipecat-food python targets/pipecat-food/bot.py --planted-bug records_wrong_size
```

| Address | What |
|---|---|
| `ws://127.0.0.1:7860/ws` | the agent (Twilio Media Streams) |
| `http://127.0.0.1:7860/health` | ready check (also says which planted bug is on) |
| `http://127.0.0.1:7870` | the restaurant's order API; orders in `orders.db` |
| `http://127.0.0.1:8790/tools` | where the agent sends its tool calls (Vocto's tool gateway; `TOOLS_URL`) |

Settings (environment, or the Vocto `.env` file): `LLM_PROVIDER` (`anthropic`, `ollama`, `rules`),
`AGENT_MODEL`, `ANTHROPIC_API_KEY`, `OLLAMA_URL`, `WHISPER_MODEL` (`base.en`), `WHISPER_TTFS_P99` (`2.5`, seconds the turn may wait for a
transcript; Pipecat assumes 1 s, which is too short for Whisper on a CPU), `PIPER_VOICE`
(`en_US-lessac-medium`), `TOOLS_URL`, `FOOD_BOT_PLANTED_BUG`. Its log is `logs/pipecat-food.log`
(with each call's test id, which Vocto collects).

| File | |
|---|---|
| `food_ordering.py` | the upstream flow; every change is marked `VOCTO:` |
| `bot.py` | the server and pipeline (upstream's `run_bot`, with local speech models) |
| `tools.py` | the agent's HTTP calls to its tools, with the call's test id |
| `restaurant_api.py` | order API and database |
| `planted_bugs.py` | the bugs Vocto's exit check plants, one at a time |
| `rules_llm.py` | a free scripted stand-in for the LLM (plumbing checks only) |
| `fixtures/base.sql` | empties the orders table before each test call |
