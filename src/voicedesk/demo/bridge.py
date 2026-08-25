"""A WebSocket bridge: the browser's microphone to Deepgram's ears, live.

Runs beside the HTTP server on the next port up. The browser opens one socket,
streams raw PCM as it is captured, and gets transcripts back as they are
recognised -- interim ones while the caller is still talking, a final one the
moment they stop.

**Why this exists, in one measurement.** The first version recorded the whole
utterance, waited 1.1 seconds to be sure the caller had finished, uploaded the
file, and waited for a batch transcript. That cost **1100ms of silence plus
3154ms of transcription** before the model had even been asked a question. With
this bridge the audio is already at Deepgram while the caller is still speaking,
and the final transcript arrives about 300ms after they stop. The recogniser is
not faster; it simply stopped being asked to start from nothing.

The key never reaches the page. Deepgram can mint short-lived browser tokens
(`/v1/auth/grant`) which would remove this hop entirely, and the key on this
account is refused for it -- `403 FORBIDDEN, insufficient permissions`. If a
key with `keys:write` becomes available, going direct is one fewer process and
one fewer network hop.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from urllib.parse import urlencode

import structlog
import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import ServerConnection, serve

log = structlog.get_logger(__name__)

DEEPGRAM_WS = "wss://api.deepgram.com/v1/listen"
SARVAM_WS = "wss://api.sarvam.ai/speech-to-text/ws"

ENDPOINTING_MS = 400
"""Silence, in ms, after which Deepgram marks a transcript `speech_final`.

Deepgram's default is 10ms, which fires mid-sentence on anyone who pauses to
think. Their own samples use 300. This sits deliberately above that, because
the tradeoff is not symmetric for a BOOKING line: false-cutoff rate and latency
are the same dial, and a caller saying "Tuesday... no, wait, Wednesday
afternoon" who gets cut off costs a full repair turn -- about three seconds --
while 150ms of extra patience costs 150ms.

LiveKit's published curve puts the cost concretely: hitting a 5% false-cutoff
rate needs ~543ms of mean latency, hitting 10% needs ~295ms. Err high here and
buy the latency back elsewhere in the pipeline, where it is free.
"""

UTTERANCE_END_MS = 1000
"""Backstop. Deepgram emits `UtteranceEnd` after this much silence even when no
`speech_final` arrived -- which happens on noisy lines where the recogniser
never becomes confident enough to close the turn. Without it a caller in a
busy corridor is never answered at all."""


def listen_url(model: str, language: str | None) -> str:
    params: dict[str, Any] = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": 16000,
        "channels": 1,
        "interim_results": "true",
        "punctuate": "true",
        # `smart_format` off, deliberately. It holds transcription back to
        # format dates and numbers prettily, and the consumer here is a model
        # that neither needs nor benefits from the prettifying -- it is being
        # asked what the caller wants, not shown a receipt. `punctuate` stays
        # on because sentence boundaries are what the endpointer reasons about.
        "smart_format": "false",
        "endpointing": ENDPOINTING_MS,
        "utterance_end_ms": UTTERANCE_END_MS,
        "vad_events": "true",
    }
    # `multi` is nova-3's code-switching mode: it transcribes a caller who
    # changes language MID-SENTENCE, which is the normal way people speak here
    # (D4 calls code-switch a first-class case, not an edge case).
    #
    # Not `detect_language=true`. That parameter exists, it is documented, and
    # the streaming socket rejects it outright with HTTP 400 -- it is a
    # pre-recorded-only option. Detection per utterance would also be the wrong
    # shape anyway: it decides once per turn, and a caller who starts a sentence
    # in English and finishes it in Tamil is one utterance, not two.
    params["language"] = language or "multi"
    return f"{DEEPGRAM_WS}?{urlencode(params)}"


class Bridge:
    """One browser socket, one Deepgram socket, pumped in both directions."""

    def __init__(self, api_key: str, model: str) -> None:
        self._key = api_key
        self._model = model

    async def handle(self, browser: ServerConnection) -> None:
        language = None
        try:
            first = await asyncio.wait_for(browser.recv(), timeout=10)
            if isinstance(first, str):
                language = (json.loads(first) or {}).get("language")
        except (TimeoutError, json.JSONDecodeError, websockets.ConnectionClosed):
            return

        url = listen_url(self._model, language)
        try:
            async with ws_connect(
                url, additional_headers={"Authorization": f"Token {self._key}"}
            ) as deepgram:
                await asyncio.gather(
                    self._to_deepgram(browser, deepgram),
                    self._to_browser(browser, deepgram),
                )
        except websockets.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001 - one caller's socket, never the process
            log.warning("bridge.failed", exc_info=True)
            await self._say(browser, {"type": "error", "message": "listening failed"})

    async def _to_deepgram(self, browser: Any, deepgram: Any) -> None:
        """Microphone frames upstream, unbuffered."""
        try:
            async for message in browser:
                if isinstance(message, bytes):
                    await deepgram.send(message)
                elif message == "done":
                    # CloseStream: flush what is pending and finalise, rather
                    # than dropping the socket and losing the last word.
                    await deepgram.send(json.dumps({"type": "CloseStream"}))
        except websockets.ConnectionClosed:
            pass

    async def _to_browser(self, browser: Any, deepgram: Any) -> None:
        """Transcripts downstream, as they are recognised."""
        try:
            async for raw in deepgram:
                event = json.loads(raw)
                kind = event.get("type")

                if kind == "Results":
                    channel = event.get("channel") or {}
                    alt = (channel.get("alternatives") or [{}])[0]
                    text = (alt.get("transcript") or "").strip()
                    if not text:
                        continue
                    await self._say(
                        browser,
                        {
                            "type": "transcript",
                            "text": text,
                            "final": bool(event.get("is_final")),
                            # `speech_final` means Deepgram believes the SENTENCE
                            # ended, not merely that this chunk is settled. It is
                            # the signal to answer; `is_final` alone is not.
                            "speech_final": bool(event.get("speech_final")),
                            "language": channel.get("detected_language"),
                            "confidence": channel.get("language_confidence"),
                        },
                    )
                elif kind == "UtteranceEnd":
                    await self._say(browser, {"type": "utterance_end"})
                elif kind == "SpeechStarted":
                    # The caller began talking. The page uses this to stop the
                    # agent mid-sentence -- barge-in, and the difference between
                    # a phone call and a walkie-talkie.
                    await self._say(browser, {"type": "speech_started"})
        except websockets.ConnectionClosed:
            pass

    @staticmethod
    async def _say(browser: Any, payload: dict[str, Any]) -> None:
        try:
            await browser.send(json.dumps(payload))
        except websockets.ConnectionClosed:
            pass


class SarvamBridge:
    """The same socket contract, spoken to Saaras instead.

    Deepgram is faster -- 313ms to a final transcript against roughly 1.1s here
    -- and it **cannot hear Tamil at all**. `nova-3` in `multi` mode covers
    English, Hindi, Spanish, French, German, Italian, Portuguese, Japanese and
    Dutch, so a Tamil caller was not failing to be understood, they were being
    forced into the nearest language it did know: "vanakkam, doctor-kitta
    appointment venum" came back as Devanagari.

    Saaras returns `ta-IN` at 0.999 on the same audio, in Tamil script. A
    recogniser that is fast and wrong is not a recogniser, so the seconds are
    the right thing to pay here -- and the LLM leg is where they should be
    bought back, not this one.

    The browser-facing protocol is unchanged, deliberately: the page does not
    know which recogniser it is talking to, which is what makes this a swap
    rather than a rewrite.
    """

    FRAME = 1024

    def __init__(self, api_key: str, model: str) -> None:
        self._key = api_key
        self._model = model or "saaras:v3"

    def url(self, language: str | None) -> str:
        params = {
            "model": self._model,
            # "unknown" is auto-detect, and it is NOT the default -- Sarvam's
            # own Pipecat plugin defaults to en-IN, which silently turns a
            # multilingual line into an English one.
            "language-code": language or "unknown",
            "vad_signals": "true",
        }
        return f"{SARVAM_WS}?{urlencode(params)}"

    async def handle(self, browser: ServerConnection) -> None:
        language = None
        try:
            first = await asyncio.wait_for(browser.recv(), timeout=10)
            if isinstance(first, str):
                language = (json.loads(first) or {}).get("language")
        except (TimeoutError, json.JSONDecodeError, websockets.ConnectionClosed):
            return

        try:
            async with ws_connect(
                self.url(language),
                additional_headers={"api-subscription-key": self._key},
                max_size=2**22,
            ) as sarvam:
                await asyncio.gather(
                    self._up(browser, sarvam), self._down(browser, sarvam)
                )
        except websockets.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001 - one caller's socket, never the process
            log.warning("bridge.sarvam_failed", exc_info=True)
            await _say(browser, {"type": "error", "message": "listening failed"})

    async def _up(self, browser: Any, sarvam: Any) -> None:
        """Microphone frames upstream, base64 inside a JSON envelope.

        Sarvam takes audio as JSON rather than binary frames, so every frame
        costs a base64 expansion. At 16kHz mono that is a few KB a second and
        not worth optimising; it is worth KNOWING, because it is the reason
        this socket carries text where Deepgram's carries bytes.
        """
        try:
            async for message in browser:
                if not isinstance(message, bytes):
                    continue
                await sarvam.send(
                    json.dumps(
                        {
                            "audio": {
                                "data": base64.b64encode(message).decode(),
                                "encoding": "audio/wav",
                                "sample_rate": 16000,
                            }
                        }
                    )
                )
        except websockets.ConnectionClosed:
            pass

    async def _down(self, browser: Any, sarvam: Any) -> None:
        """Saaras events, translated into the page's vocabulary."""
        try:
            async for raw in sarvam:
                event = json.loads(raw)
                kind = event.get("type")
                data = event.get("data") or {}

                if kind == "events":
                    signal = data.get("signal_type")
                    if signal == "START_SPEECH":
                        await _say(browser, {"type": "speech_started"})
                    elif signal == "END_SPEECH":
                        # The turn is over acoustically; the words are still
                        # being decoded. Nothing to tell the page yet -- saying
                        # "utterance_end" here would answer before the
                        # transcript arrives, with nothing in it.
                        pass
                    continue

                if kind == "data":
                    text = (data.get("transcript") or "").strip()
                    if not text:
                        continue
                    confident = (
                        data.get("language_probability") or 0
                    ) >= LANGUAGE_CONFIDENCE
                    await _say(
                        browser,
                        {
                            "type": "transcript",
                            "text": text,
                            "final": True,
                            # Saaras only emits a transcript once the turn has
                            # closed, so every one it sends is a finished
                            # sentence. There is no interim stream to reconcile.
                            "speech_final": True,
                            "language": data.get("language_code") if confident else None,
                            "confidence": data.get("language_probability"),
                        },
                    )
        except websockets.ConnectionClosed:
            pass


LANGUAGE_CONFIDENCE = 0.55
"""Below this the provider is guessing, and its guess is discarded.

Saaras put clean Tamil at 0.999. Deepgram put a sentence it could not place at
0.14 -- as Swedish. There is a great deal of room between those and the line
sits in it."""


async def _say(browser: Any, payload: dict[str, Any]) -> None:
    try:
        await browser.send(json.dumps(payload))
    except websockets.ConnectionClosed:
        pass


async def run(
    api_key: str, model: str, port: int, provider: str = "deepgram"
) -> None:
    bridge: Any = (
        SarvamBridge(api_key, model) if provider == "sarvam" else Bridge(api_key, model)
    )
    async with serve(bridge.handle, "127.0.0.1", port, max_size=2**22):
        await asyncio.Future()


def start_in_thread(
    api_key: str, model: str, port: int, provider: str = "deepgram"
) -> None:
    """Run the bridge alongside the blocking HTTP server.

    Its own thread and its own event loop: `ThreadingHTTPServer` occupies the
    main thread and knows nothing about asyncio, and the two must not be made
    to share a loop just to live in one process.
    """
    import threading

    def go() -> None:
        asyncio.run(run(api_key, model, port, provider))

    threading.Thread(target=go, daemon=True, name="deepgram-bridge").start()
