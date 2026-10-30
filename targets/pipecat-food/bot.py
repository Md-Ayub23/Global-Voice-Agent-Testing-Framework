"""Pipecat's food ordering agent as a phone agent, on this computer, with free local speech models.

This is what a restaurant would deploy behind a Twilio phone number: a FastAPI server whose
WebSocket takes Twilio Media Streams (Pipecat's own TwilioFrameSerializer), then Pipecat's pipeline
(speech to text, the LLM with the food ordering flow, text to speech). Vocto calls it the way Twilio
would, so nothing here knows it is being tested.

    uv run --project targets/pipecat-food python targets/pipecat-food/bot.py

  ws://127.0.0.1:7860/ws        the agent (Twilio Media Streams format)
  http://127.0.0.1:7860/health  ready check
  http://127.0.0.1:7870         the restaurant's order API (restaurant_api.py) and orders.db

Speech: faster-whisper and Piper, run locally (no keys). LLM: Claude (ANTHROPIC_API_KEY, read from
the Vocto .env file) by default, or a local Ollama model with LLM_PROVIDER=ollama (free), or
LLM_PROVIDER=rules: a scripted stand-in that only checks the plumbing (rules_llm.py).

Settings (environment): LLM_PROVIDER anthropic | ollama | rules, AGENT_MODEL (default claude-haiku-5-5, or
llama3.1 for Ollama), OLLAMA_URL, WHISPER_MODEL (base.en), PIPER_VOICE (en_US-lessac-medium),
TOOLS_URL (default Vocto's tool gateway), FOOD_BOT_PLANTED_BUG (see planted_bugs.py).
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))


def load_env(path: Path) -> None:
    """KEY=VALUE lines of the Vocto .env file; variables already set in the shell win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.removeprefix("export ").partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(ROOT / ".env")


def _allow_missing_pyav() -> None:
    """faster-whisper imports PyAV only to decode files; Pipecat hands it samples. Windows Smart App
    Control can block PyAV's DLLs, so if it can't load, a placeholder stands in (as Vocto does)."""
    import types

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        import av  # noqa: F401
    except (ImportError, OSError) as e:
        cause = str(e)
        placeholder = types.ModuleType("av")

        def missing(name):
            raise ImportError(f"PyAV is not available ({cause})")

        placeholder.__getattr__ = missing  # type: ignore[method-assign]
        sys.modules["av"] = placeholder


_allow_missing_pyav()

import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from loguru import logger  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.flows import FlowManager  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineParams, PipelineWorker  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.utils import parse_telephony_websocket  # noqa: E402
from pipecat.serializers.twilio import TwilioFrameSerializer  # noqa: E402
from pipecat.services.piper import tts as piper_tts  # noqa: E402
from pipecat.services.piper.tts import PiperTTSService  # noqa: E402
from pipecat.services.whisper.stt import WhisperSTTService  # noqa: E402
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402

import food_ordering  # noqa: E402
import planted_bugs  # noqa: E402
import restaurant_api  # noqa: E402
import tools  # noqa: E402

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base.en")
WHISPER_TTFS_P99 = float(os.getenv("WHISPER_TTFS_P99", "2.5"))   # seconds; see make_stt
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_US-lessac-medium")
MODELS = ROOT / ".models"               # the same model folders Vocto uses, so nothing downloads twice
PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()

# Keep the speech models loaded between calls, as a deployed agent does (Pipecat's services load
# them in their constructors, once per call).
piper_tts.PiperVoice.load = functools.lru_cache(maxsize=4)(piper_tts.PiperVoice.load)


class LoadedWhisperSTT(WhisperSTTService):
    _models: dict[tuple, object] = {}

    def _load(self):
        from faster_whisper import WhisperModel

        key = (self._settings.model, self._device, self._compute_type)
        if key not in self._models:
            self._models[key] = WhisperModel(self._settings.model, device=self._device, compute_type=self._compute_type,
                                             download_root=str(MODELS / "whisper"))
        self._model = self._models[key]
        unsupported = self._unsupported_language()
        if unsupported:
            raise ValueError(unsupported)


def make_stt() -> WhisperSTTService:
    # How long the turn may wait for Whisper's transcript after the caller stops. Pipecat assumes 1 s
    # (Whisper on a GPU); on a CPU a segment takes up to ~1.8 s. With 1 s, a caller who pauses between
    # two sentences got two replies: the turn ended with only the first sentence's text, and ended again
    # when the second sentence's transcript came in. A transcript that covers all the caller said ends
    # the turn at once, so this only matters when it is late.
    return LoadedWhisperSTT(device="cpu", compute_type="int8", ttfs_p99_latency=WHISPER_TTFS_P99,
                            settings=WhisperSTTService.Settings(model=WHISPER_MODEL))


def make_tts() -> PiperTTSService:
    return PiperTTSService(download_dir=MODELS / "piper", settings=PiperTTSService.Settings(voice=PIPER_VOICE))


_rules_url: str | None = None


def make_llm():
    global _rules_url
    if PROVIDER == "rules":                 # a scripted stand-in, for a free plumbing check (rules_llm.py)
        import rules_llm
        from pipecat.services.openai.llm import OpenAILLMService

        _rules_url = _rules_url or rules_llm.serve()
        return OpenAILLMService(base_url=_rules_url, api_key="none", settings=OpenAILLMService.Settings(model="rules"))
    if PROVIDER == "ollama":
        from pipecat.services.ollama.llm import OLLamaLLMService

        return OLLamaLLMService(base_url=os.getenv("OLLAMA_URL", "http://localhost:11434/v1"),
                                settings=OLLamaLLMService.Settings(model=os.getenv("AGENT_MODEL", "llama3.1")))
    if PROVIDER == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("set ANTHROPIC_API_KEY in the .env file, or use LLM_PROVIDER=ollama")
        return AnthropicLLMService(api_key=key,
                                   settings=AnthropicLLMService.Settings(model=os.getenv("AGENT_MODEL", "claude-haiku-5-5")))
    raise SystemExit(f"LLM_PROVIDER must be anthropic, ollama or rules, got {PROVIDER!r}")


app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True, "agent": "Pipecat food ordering", "planted_bug": os.getenv("FOOD_BOT_PLANTED_BUG") or None}


@app.websocket("/ws")
async def call(websocket: WebSocket):
    await websocket.accept()
    _, call_data = await parse_telephony_websocket(websocket)
    params = {str(k): str(v) for k, v in dict(call_data.body or {}).items()}
    tools.call_params.set(params)   # every task started from here (the whole pipeline) sees it
    logger.info(f"call {params.get('test_id', '-')} started (stream {call_data.stream_id})")
    serializer = TwilioFrameSerializer(stream_sid=call_data.stream_id, call_sid=call_data.call_id,
                                       params=TwilioFrameSerializer.InputParams(auto_hang_up=False))
    transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer))
    await run_bot(transport)
    logger.info(f"call {params.get('test_id', '-')} ended")


async def run_bot(transport):
    """Upstream's run_bot (food_ordering.py), with local speech models and no Daily/WebRTC runner."""
    stt, tts, llm = make_stt(), make_tts(), make_llm()

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            filter_incomplete_user_turns=True,
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    flow_manager = FlowManager(
        worker=worker,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
        global_functions=[food_ordering.get_delivery_estimate],
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await flow_manager.initialize(food_ordering.create_initial_node())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


def main() -> int:
    p = argparse.ArgumentParser(description="Pipecat's food ordering agent, answering Twilio-format calls.")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--api-port", type=int, default=restaurant_api.PORT)
    p.add_argument("--no-api", action="store_true", help="don't start the restaurant order API here")
    p.add_argument("--planted-bug", default=os.getenv("FOOD_BOT_PLANTED_BUG", ""),
                   help=f"start with one planted bug: {', '.join(planted_bugs.BUGS)}")
    p.add_argument("--log", default=str(ROOT / "logs" / "pipecat-food.log"))
    args = p.parse_args()
    os.environ["FOOD_BOT_PLANTED_BUG"] = args.planted_bug

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logger.add(args.log, level="INFO", format="{time:HH:mm:ss} {level} {message}")
    logging.getLogger("restaurant-api").addHandler(logging.FileHandler(args.log, encoding="utf-8"))
    logging.getLogger("restaurant-api").setLevel(logging.INFO)

    planted_bugs.apply(food_ordering, args.planted_bug or None)
    make_llm()                                          # stop now if the LLM is not set up
    print("loading speech models (the first start downloads them)...", flush=True)
    make_stt(), make_tts()
    if not args.no_api:
        restaurant_api.serve(args.api_port)
        print(f"restaurant order API: http://127.0.0.1:{args.api_port}  (orders in {restaurant_api.DB_PATH})", flush=True)
    print(f"tool calls go to {tools.TOOLS_URL}", flush=True)
    if args.planted_bug:
        print(f"PLANTED BUG ON: {args.planted_bug}: {planted_bugs.BUGS[args.planted_bug]}", flush=True)
    print(f"food ordering agent: ws://127.0.0.1:{args.port}/ws  (Twilio Media Streams)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
