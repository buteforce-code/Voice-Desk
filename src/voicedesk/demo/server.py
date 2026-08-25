"""A local demo server: speak to the agent in a browser, hear it answer.

    python -m voicedesk.demo.server

**What is real here and what is not.** The agent is real -- the same
`Agent.turn` the eval suite scores, the same `ToolRegistry`, the same state
machine, the same output-side clinical guard, the same audit log.

The speech is real too, and it is **not the production stack.** STT is
ElevenLabs Scribe and TTS is an ElevenLabs voice; Sarvam Saaras and Bulbul are
the chosen production path (PROJECT.md D1) and no code in this repo calls them
yet. That is a swap behind one seam -- `_stt` and `_tts` are the whole surface
-- and it is a swap that has not been made or measured. Sarvam is chosen for
Indian-language telephony at 8kHz, which is not the same problem as a laptop
microphone at 48kHz, and nothing here says anything about how it will do.

Without an `ELEVENLABS_API_KEY` the page falls back to the browser's own
speech, which cannot detect language and so answers everyone in English. The
page says which of the two it is using.

Standard library plus `httpx`, which the project already depends on. FastAPI
would suit a service that outlives a demo; adding a framework the hour before
one is not a trade worth making. One caller at a time, in memory, no database.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import structlog

from voicedesk.adapters.memory import InMemoryAdapter
from voicedesk.agent import Agent
from voicedesk.audit import InMemoryAudit
from voicedesk.config import ConfigError, Settings
from voicedesk.llm import build_from_settings
from voicedesk.prompts import MULTILINGUAL_INVITE, PROMPT_VERSION
from voicedesk.state import CallSession, VersionStamp
from voicedesk.tenants import load_tenants
from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.scheduling import TenantConfig, register_scheduling_tools
from voicedesk.voice.pacing import FILL_AFTER_MS, HOLD_LINES
from voicedesk.voice.speech import for_speech, sentences

log = structlog.get_logger(__name__)

HERE = Path(__file__).parent
REPO = HERE.parent.parent.parent

DEMO_ANI = "+919876543210"
"""Fictional, valid Indian mobile. A real leg carries the ANI from telephony;
there is none here, so it is stated rather than defaulted somewhere quiet.
`confirm_booking` refuses without one -- see PROJECT.md D24."""

TERMINAL_STATES = {"wrap", "transfer", "abandoned", "failed", "refused"}

ELEVENLABS_TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
ELEVENLABS_STT = "https://api.elevenlabs.io/v1/speech-to-text"

DEEPGRAM_STT = "https://api.deepgram.com/v1/listen"
DEEPGRAM_TTS = "https://api.deepgram.com/v1/speak"
SARVAM_TTS = "https://api.sarvam.ai/text-to-speech"
SARVAM_STT = "https://api.sarvam.ai/speech-to-text"

HTTP = httpx.Client(
    timeout=httpx.Timeout(45.0, connect=6.0),
    limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=300.0),
)
"""One pooled client for every speech call, kept warm for the whole process.

Measured, because it looked like a micro-optimisation and is not: a fresh
`httpx.post` pays a DNS lookup and a TLS handshake per request, and the same
Deepgram synthesis took **3.5s cold and 2.2s warm**. Two speech calls a turn
means roughly 2.4 seconds of every gap was handshakes -- more than the model
spends thinking on a simple turn.

A caller hears that as the desk being slow. It was the socket.
"""

SUPPORTED = ("en-IN", "ta-IN", "hi-IN")

LANGUAGE_CONFIDENCE = 0.55
"""Below this, the provider is guessing and its guess is discarded.

Measured, not picked: Scribe put clean English at 0.92 and identified it
exactly, and put a line it could not place at 0.14 -- as Swedish. There is a
lot of room between those two numbers and the threshold sits in it.
"""

VOICE_BY_LANGUAGE = {
    "ta-IN": "gJvkwI7wGFW2czmyfJhp",
}
"""A different voice per language, where one exists that suits it.

`eleven_multilingual_v2` will read Tamil in any voice, and an American voice
reading Tamil sounds like an American reading Tamil. It is not a subtle effect:
asked to transcribe the default voice speaking a Tamil sentence, ElevenLabs'
own speech-to-text guessed **Swedish, at 0.14 confidence**, because the audio
does not sound like any language it knows. A caller would hear the same thing
and conclude the clinic had bought something cheap.

Anything not listed falls through to `ELEVENLABS_VOICE_ID`. Voice ids belong in
config, not here -- this map is a demo default so the thing sounds right out of
the box, and `ELEVENLABS_VOICE_TA` overrides it.
"""

_TAMIL = range(0x0B80, 0x0C00)
_DEVANAGARI = range(0x0900, 0x0980)


def detect_language(text: str) -> str | None:
    """Which of the three the caller is writing in, by script.

    Crude on purpose and used only as a fallback: when speech-to-text reports
    the language itself, that answer wins. A script check cannot tell Hindi
    from Marathi, and it reads romanised Tamil -- "enakku appointment venum",
    which is how a great many people actually type -- as English. Both are
    acceptable failures for a fallback and would not be for a control.
    """
    for ch in text:
        code = ord(ch)
        if code in _TAMIL:
            return "ta-IN"
        if code in _DEVANAGARI:
            return "hi-IN"
    return None


def to_supported(code: str | None) -> str | None:
    """Map whatever the STT provider calls a language onto our three.

    Scribe returns ISO-639 ("ta", "hin", "en"), sometimes with a region. Anything
    outside the three the clinic supports returns None, and the caller keeps the
    language they already had rather than being answered in something nobody
    chose.
    """
    if not code:
        return None
    base = code.replace("_", "-").split("-")[0].lower()
    return {
        "en": "en-IN", "eng": "en-IN",
        "ta": "ta-IN", "tam": "ta-IN",
        "hi": "hi-IN", "hin": "hi-IN",
    }.get(base)

BLOCKED_DAY_OFFSETS = (3, 4, 9)
"""Days from today that the demo clinic shows as fully committed.

Dummy, and labelled as such in the UI. A calendar where every square is green
says nothing: the point of showing availability to a doctor is that the agent
books around what is already taken, so some of it has to be taken. These are
offsets rather than dates so the demo looks the same whenever it is run.

They block the CALENDAR VIEW only -- the slots still exist in the adapter, so
nothing about scoring or the eval fixtures changes. A real integration reads
this from the clinic's own leave and OT roster.
"""


@dataclass
class Call:
    """One browser tab's call. Everything it needs, shared with nothing."""

    agent: Agent
    session: CallSession
    adapter: InMemoryAdapter
    tools: list[dict[str, str]] = field(default_factory=list)

    @property
    def appointments(self) -> list[dict[str, str]]:
        return [
            {
                "doctor": a.doctor_name,
                "specialty": a.specialty,
                "starts_at": a.starts_at.astimezone().strftime("%a %d %b, %H:%M"),
            }
            for a in self.adapter.appointments.values()
            if a.status == "confirmed"
        ]


class Desk:
    """Builds calls and holds the ones in flight.

    The model client is shared; everything under it is per-call, for the same
    reason `evals/world.py` isolates a case -- a reused adapter carries the
    previous call's appointments, and the next caller finds a booking they
    never made.
    """

    def __init__(self) -> None:
        settings = Settings.load()
        model = build_from_settings(settings)
        if model is None:
            raise ConfigError(
                "No model configured. Set OPENROUTER_API_KEY (or "
                "GOOGLE_AI_API_KEY) in .env -- this drives the real agent, and "
                "there is nothing to demo without one."
            )
        self.settings = settings
        self.model = model
        # The nine-doctor roster, not the twenty-five-doctor eval fixture.
        # Availability is only legible on a diary a person can read.
        slug = os.environ.get("DEMO_TENANT", "meridian_demo")
        self.tenant = load_tenants(REPO / "config" / "tenants")[slug]
        self.calls: dict[str, Call] = {}
        self._lock = threading.Lock()

        self.register = InMemoryAdapter.seeded(self.tenant)
        """ONE register for the whole clinic, shared by every call.

        It was one per call, and that was wrong in a way only a second call
        reveals: book a slot, hang up, ring again, and the same slot was free,
        because the second call had been handed a fresh diary. Two patients,
        one chair, and nothing anywhere to notice.

        The adapter already refuses a double-book -- it mirrors the partial
        unique index in `0001_init.sql` -- so the check was never missing. It
        simply had nothing to check against.

        The isolation it replaces is right for the EVAL harness and wrong here.
        `evals/world.py` gives each case its own world on purpose: a reused
        adapter there carries the previous case's appointments and the next
        case double-books, which is a harness defect that reads exactly like an
        agent defect. A clinic is the opposite -- the whole point of a register
        is that it remembers.
        """

    def start(self, language: str, ani: str) -> tuple[str, str, Call]:
        adapter = self.register
        audit = InMemoryAudit()
        registry = ToolRegistry(audit)
        register_scheduling_tools(
            registry, adapter, TenantConfig.from_tenant(self.tenant)
        )
        session = CallSession(
            clinic_id=self.tenant.clinic_id,
            call_id=uuid4(),
            trace_id=f"demo-{uuid4().hex[:8]}",
            versions=VersionStamp(PROMPT_VERSION, self.settings.llm_model),
            dry_run=False,
            ani=ani,
        )
        agent = Agent(
            tenant=self.tenant,
            session=session,
            registry=registry,
            model=self.model,
            audit=audit,
            language=language,
        )
        call = Call(agent=agent, session=session, adapter=adapter)
        # The invite goes on the FIRST line and nowhere else. A caller needs to
        # know once that Tamil and Hindi are welcome; being told every turn is
        # what an automated line sounds like, and the point is to not sound
        # like one. It is appended here rather than in `prompts.py` because it
        # belongs to this demo surface -- a real inbound leg knows the caller's
        # region and can often skip it entirely.
        greeting = agent.open() + " " + MULTILINGUAL_INVITE
        with self._lock:
            self.calls[session.trace_id] = call
        return session.trace_id, greeting, call

    def get(self, call_id: str) -> Call | None:
        with self._lock:
            return self.calls.get(call_id)

    # -- the availability view -------------------------------------------

    def day(self, day: str, specialty: str | None = None) -> list[dict[str, Any]]:
        """Every free slot on one date, as a Calendly-style column of times.

        Read-only, and that is a design position rather than an omission. A
        click here could write an appointment directly, and then the register
        would hold rows that never passed the state machine, the identity
        challenge or the audit log -- the whole of G4 routed around by a button.
        The calendar shows the clinic's day; the agent books into it.
        """
        adapter = self.register
        zone = ZoneInfo(self.tenant.timezone)
        try:
            wanted = date.fromisoformat(day)
        except ValueError:
            return []

        blocked = {
            date.today() + timedelta(days=n) for n in BLOCKED_DAY_OFFSETS
        }
        if wanted in blocked:
            return []

        seen: dict[str, dict[str, Any]] = {}
        for slot in adapter.slots.values():
            local = slot.starts_at.astimezone(zone)
            if local.date() != wanted:
                continue
            if specialty and slot.specialty.lower() != specialty.lower():
                continue
            key = local.strftime("%H:%M")
            # `%-I` strips the leading zero on glibc and raises on Windows,
            # which is where this runs. Formatted by hand instead.
            hour12 = local.hour % 12 or 12
            label = f"{hour12}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"
            row = seen.setdefault(
                key, {"time": key, "label": label, "doctors": []}
            )
            if slot.doctor_name not in row["doctors"]:
                row["doctors"].append(slot.doctor_name)
        return [seen[k] for k in sorted(seen)]

    def calendar(self, specialty: str | None = None, days: int = 21) -> list[dict[str, Any]]:
        """A fortnight-and-a-bit of the clinic's day, as a doctor reads it.

        Built from the same seeded adapter a call books against, so the squares
        and the agent cannot disagree -- a calendar drawn from a second source
        is a calendar that lies the first time someone books.
        """
        adapter = self.register
        zone = ZoneInfo(self.tenant.timezone)
        today = datetime.now(zone).date()
        blocked = {today + timedelta(days=n) for n in BLOCKED_DAY_OFFSETS}

        free: dict[date, int] = defaultdict(int)
        for slot in adapter.slots.values():
            if specialty and slot.specialty.lower() != specialty.lower():
                continue
            free[slot.starts_at.astimezone(zone).date()] += 1

        # What this call actually did to the clinic's day. Counted from the
        # same register the agent books into, so a booking shows up here the
        # moment it lands -- the calendar was reading a freshly seeded adapter
        # and could never have shown one.
        booked: dict[date, int] = defaultdict(int)
        for appt in adapter.appointments.values():
            if appt.status != "confirmed":
                continue
            if specialty and str(appt.specialty).lower() != specialty.lower():
                continue
            booked[appt.starts_at.astimezone(zone).date()] += 1

        out: list[dict[str, Any]] = []
        for offset in range(days):
            day = today + timedelta(days=offset)
            count = free.get(day, 0)
            if day in blocked and count:
                status, count = "blocked", 0
            elif count == 0:
                status = "closed"
            elif count <= 12:
                status = "limited"
            else:
                status = "open"
            out.append({
                "date": day.isoformat(),
                "day": day.day,
                "weekday": day.strftime("%a"),
                "month": day.strftime("%b"),
                "free": count,
                "booked": booked.get(day, 0),
                "status": status,
                "today": day == today,
            })
        return out


def _snapshot(
    call: Call,
    *,
    reply: str = "",
    blocked: bool = False,
    categories: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "reply": reply,
        # The same utterance, rendered for a synthesiser and cut at sentence
        # boundaries. Two separate wins and the page needs both:
        #
        #   latency -- Bulbul returns a whole base64 document, so there is
        #   nothing to stream and a two-sentence reply stays silent until the
        #   second sentence exists. Split, time-to-first-audio is sentence one
        #   alone and the rest synthesises behind it.
        #
        #   prosody -- two clips have a real gap between them where a person
        #   would breathe. Synthesised as one blob they run together at the
        #   full stop, which is most of what makes a read-back sound recited.
        #
        # Split here rather than in the page because the rules are language
        # ones -- the Devanagari danda is a sentence terminator and a
        # JavaScript regex written from English habits does not know that.
        "clips": _clips(call, reply),
        "state": call.session.state.value,
        "states": list(call.session.history()),
        "terminal": call.session.state.value in TERMINAL_STATES,
        "identity_verified": call.session.identity_verified,
        "tools": call.tools,
        "appointments": call.appointments,
        "blocked": blocked,
        "categories": list(categories),
    }


def _clips(call: Call, reply: str) -> list[str]:
    """The reply as the synthesiser should receive it, one sentence per clip.

    Never raises. A renderer fault must degrade to a slightly robotic sentence
    and never to a silent turn -- silence is the one output a voice line may
    not produce, and this runs on every turn of every call.
    """
    if not reply:
        return []
    try:
        return sentences(
            for_speech(
                reply,
                call.agent.language,
                timezone=call.agent.tenant.timezone,
            )
        )
    except Exception:  # noqa: BLE001 - see the docstring
        log.warning("demo.speech_render_failed", exc_info=True)
        return [reply]


def _int_env(name: str, default: int) -> int:
    """A positive integer from the environment, or the default.

    Never raises. `config.Settings` is strict because a misconfigured pipeline
    must not boot; this governs a demo's courtesy ceiling, and a typo in a
    Railway variable should not take the demo down at 2am.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("demo.bad_env", name=name, value=raw)
        return default
    return value if value > 0 else default


ALLOWED_ORIGINS = tuple(
    o.strip().rstrip("/")
    for o in (os.environ.get("DEMO_ALLOWED_ORIGINS") or "").split(",")
    if o.strip()
)
"""Which sites may call this backend from a browser.

Empty means same-origin only, which is what a local run wants and what a
deployment must not be left on by accident. The marketing site is served from
Vercel and this service runs on Railway, so they are different origins by
construction -- and the whole reason for that split is the residency rule: a
caller's words go from their browser to India and never touch the CDN that
served the HTML.

A comma-separated list, not a wildcard, because `*` here would let any page on
the internet spend this project's Sarvam and model credits.
"""

CALL_BUDGET = _int_env("DEMO_CALLS_PER_HOUR", 12)
TURN_BUDGET = _int_env("DEMO_TURNS_PER_HOUR", 90)
"""What one visitor may spend in an hour.

Every turn is a model round-trip and every reply is several synthesis calls,
all of them billed. A public demo with no ceiling is a public demo with
somebody else's credit card, and the first person to notice will be a script.

Generous enough that a curious visitor never hits it: twelve calls is far more
than anyone tries before deciding whether they like it.
"""


class Budget:
    """A fixed-window counter per client, in memory.

    Deliberately not a sliding window and deliberately not Redis. This guards
    a demo against a scraper and a stuck retry loop, not against a determined
    adversary -- and a rate limiter that needs its own datastore is a second
    thing that can take the demo down.

    One process, one dict. Railway runs a single container here; if that ever
    becomes two, this becomes per-container and the ceiling doubles. Written
    down because that is exactly the kind of thing that is discovered from a
    bill.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, who: str, kind: str, ceiling: int) -> bool:
        now = time.monotonic()
        cutoff = now - 3600
        with self._lock:
            hits = self._seen[(who, kind)]
            hits[:] = [t for t in hits if t > cutoff]
            if len(hits) >= ceiling:
                return False
            hits.append(now)
            return True


class Handler(BaseHTTPRequestHandler):
    desk: Desk
    budget = Budget()

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence per-request logging. The interesting trace is in the UI."""

    # -- plumbing --------------------------------------------------------

    def _client(self) -> str:
        """Who is calling, as far as we can tell behind Railway's proxy.

        `client_address` is the proxy, so every visitor would share one bucket
        and the first of them would exhaust it for everybody. The left-most
        entry of `X-Forwarded-For` is the original client.

        Spoofable, and that is acceptable here: the header is trusted because
        the only thing it gates is a courtesy ceiling on a free demo. It must
        never be trusted for anything that authorises.
        """
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _origin(self) -> str | None:
        origin = (self.headers.get("Origin") or "").rstrip("/")
        return origin if origin and origin in ALLOWED_ORIGINS else None

    def _cors(self) -> None:
        origin = self._origin()
        if not origin:
            return
        # Echoed, never `*`: the list is the allowlist, and echoing keeps the
        # response correct for whichever of several origins asked.
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        self.send_response(204 if self._origin() else 403)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            # Railway polls this before routing traffic to a new container.
            # It answers only once `Desk()` has built -- the tenant is loaded
            # and the model client exists -- because an instance that 200s
            # while still warming takes a real call and drops it.
            # `str()`, because `clinic_id` is a UUID and `json.dumps` refuses
            # one -- which turned the health check into a 500 and would have
            # left Railway refusing to route to a container that was fine.
            return self._json({"ok": True, "clinic": str(self.desk.tenant.clinic_id)})
        if path in ("/", "/index.html"):
            return self._file(HERE / "index.html")
        if path == "/api/config":
            return self._json(
                {
                    "clinic": self.desk.tenant.display_name,
                    "model": self.desk.settings.llm_model,
                    "prompt_version": PROMPT_VERSION,
                    "specialties": list(self.desk.tenant.active_specialties()),
                    "ani": DEMO_ANI,
                    "voice": self.desk.settings.speech_provider,
                    "live_stt": bool(
                        self.desk.settings.sarvam_api_key
                        or self.desk.settings.deepgram_api_key
                    ),
                    "bridge": f"ws://127.0.0.1:{self.server.server_address[1] + 1}",
                    # What the desk says while it is working, and how long the
                    # line may be silent first. Served rather than hardcoded in
                    # the page because these are caller-facing lines in three
                    # languages -- the same reason the disclosure and the
                    # opening question live in `prompts.py`. One place a Tamil
                    # speaker can correct the Tamil.
                    "hold_lines": HOLD_LINES,
                    "fill_after_ms": FILL_AFTER_MS,
                }
            )
        if path == "/api/tts":
            # GET as well as POST, because `new Audio(url)` streams a GET
            # natively -- the browser starts playing on the first bytes and
            # handles buffering itself. Fetching a blob and playing it after
            # waits for the last byte instead, which is the whole gap.
            query = parse_qs(self.path.partition("?")[2])
            return self._tts(
                {
                    "text": (query.get("text") or [""])[0],
                    "language": (query.get("language") or [""])[0],
                }
            )
        if path == "/api/day":
            query = parse_qs(self.path.partition("?")[2])
            day = (query.get("date") or [""])[0]
            specialty = (query.get("specialty") or [""])[0] or None
            if not day:
                return self._json({"error": "date required"}, 400)
            return self._json({"date": day, "slots": self.desk.day(day, specialty)})
        if path == "/api/calendar":
            query = parse_qs(self.path.partition("?")[2])
            specialty = (query.get("specialty") or [""])[0] or None
            return self._json({"days": self.desk.calendar(specialty)})
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        try:
            if path == "/api/start":
                return self._start()
            if path == "/api/turn":
                return self._turn()
            if path == "/api/stt":
                return self._stt()
            if path == "/api/tts":
                return self._tts()
        except Exception as exc:  # noqa: BLE001 - a demo must not fail silently
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        self._send(404, b"not found", "text/plain")

    def _file(self, path: Path) -> None:
        if not path.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def _start(self) -> None:
        if not self.budget.allow(self._client(), "call", CALL_BUDGET):
            return self._json(
                {
                    "error": "You've had a good go at the demo line for one hour. "
                    "Try again later, or get in touch and we'll show you properly."
                },
                429,
            )
        body = self._read_json()
        call_id, greeting, call = self.desk.start(
            body.get("language") or "en-IN", body.get("ani") or DEMO_ANI
        )
        payload = _snapshot(call, reply=greeting)
        payload["call_id"] = call_id
        self._json(payload)

    def _turn(self) -> None:
        if not self.budget.allow(self._client(), "turn", TURN_BUDGET):
            return self._json(
                {"error": "That's a lot of turns for one hour — give it a rest and come back."},
                429,
            )
        body = self._read_json()
        call = self.desk.get(body.get("call_id") or "")
        if call is None:
            return self._json(
                {"error": "no such call - reload the page to start a new one"}, 404
            )

        text = (body.get("text") or "").strip()
        if not text:
            return self._json({"error": "nothing was said"}, 400)
        if call.session.state.value in TERMINAL_STATES:
            return self._json(
                {"error": f"this call already ended in '{call.session.state.value}'"},
                409,
            )

        # The caller's language, per turn, from whatever knows best: the STT
        # provider's own detection, else the script they typed in. Nobody is
        # asked to declare it, and a caller who switches mid-call is followed
        # rather than corrected -- code-switching is the norm here, not the
        # edge case (PROJECT.md D4).
        language = to_supported(body.get("language")) or detect_language(text)
        if language and language != call.agent.language:
            call.agent.language = language

        trace = asyncio.run(call.agent.turn(text))
        call.tools = [{"name": name, "code": code} for name, code in trace.tool_calls]
        payload = _snapshot(
            call,
            reply=trace.spoken_text,
            blocked=trace.clinical_blocked,
            categories=trace.clinical_categories,
        )
        payload["language"] = call.agent.language
        self._json(payload)

    def _stt(self) -> None:
        """Audio in, text out, with the language the caller actually used.

        Server-side because the browser's recogniser must be TOLD its language
        before it hears anything -- `SpeechRecognition` set to `en-IN` renders
        fluent Tamil as English-sounding nonsense. A dropdown is the usual way
        round that, and asking a patient to declare their language before
        speaking is precisely the friction a voice line exists to remove.
        Scribe detects it instead, which is what makes the dropdown removable.
        """
        settings = self.desk.settings
        length = int(self.headers.get("Content-Length") or 0)
        audio = self.rfile.read(length) if length else b""
        if not audio:
            return self._json({"error": "no audio"}, 400)

        ctype = self.headers.get("X-Audio-Type") or "audio/webm"

        if settings.sarvam_api_key:
            return self._stt_sarvam(audio, ctype, settings)
        if settings.deepgram_api_key:
            return self._stt_deepgram(audio, ctype, settings)

        key = settings.elevenlabs_api_key
        if not key:
            return self._json({"error": "no speech key configured"}, 503)
        try:
            response = HTTP.post(
                ELEVENLABS_STT,
                headers={"xi-api-key": key},
                files={"file": ("turn.webm", audio, ctype)},
                data={"model_id": "scribe_v1"},
                timeout=45.0,
            )
        except httpx.HTTPError as exc:
            return self._json({"error": f"speech-to-text unreachable: {exc}"}, 502)

        if response.status_code != 200:
            return self._json(
                {"error": f"speech-to-text refused ({response.status_code})"}, 502
            )

        body = response.json()
        text = (body.get("text") or "").strip()

        # Scribe reports its own confidence, and a guess is not a detection.
        # Asked to identify a sentence it could not place, it answered "swe"
        # -- Swedish -- at 0.14 on a Tamil line. Acting on that would switch a
        # Chennai caller into a language nobody in the call speaks, and the
        # only way back is for them to give up and start again.
        #
        # Below the threshold the script check decides, and if that is also
        # silent the caller simply keeps the language they already had. Doing
        # nothing is the right answer far more often than guessing is.
        confident = (body.get("language_probability") or 0) >= LANGUAGE_CONFIDENCE
        detected = (
            to_supported(body.get("language_code")) if confident else None
        ) or detect_language(text)
        self._json({"text": text, "language": detected})

    def _stt_deepgram(self, audio: bytes, ctype: str, settings: Any) -> None:
        """Deepgram Listen. Raw audio in, transcript plus detected language out.

        `detect_language=true` is what removes the dropdown, exactly as Scribe
        did -- and Deepgram reports a confidence for the guess too, so the same
        threshold applies. `smart_format` gives punctuation and spoken numbers
        as digits, which matters when the next thing to happen is a model
        reading "four thirty" out of a sentence.
        """
        try:
            response = HTTP.post(
                DEEPGRAM_STT,
                params={
                    "model": settings.deepgram_stt_model,
                    "detect_language": "true",
                    "smart_format": "true",
                    "punctuate": "true",
                },
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": ctype,
                },
                content=audio,
                timeout=45.0,
            )
        except httpx.HTTPError as exc:
            return self._json({"error": f"speech-to-text unreachable: {exc}"}, 502)

        if response.status_code != 200:
            return self._json(
                {"error": f"speech-to-text refused ({response.status_code})"}, 502
            )

        body = response.json()
        channels = (body.get("results") or {}).get("channels") or []
        if not channels:
            return self._json({"text": "", "language": None})

        channel = channels[0]
        alternatives = channel.get("alternatives") or [{}]
        text = (alternatives[0].get("transcript") or "").strip()

        confident = (channel.get("language_confidence") or 0) >= LANGUAGE_CONFIDENCE
        detected = (
            to_supported(channel.get("detected_language")) if confident else None
        ) or detect_language(text)
        self._json({"text": text, "language": detected})

    def _stream_audio(self, request: Any) -> bool:
        """Forward an upstream audio response to the browser as it arrives.

        The difference this makes is the whole of it: `/v1/speak` synthesises a
        sentence in about two seconds and, read with `.content`, none of those
        bytes reach the caller until the last one exists. Streamed, the browser
        starts playing the moment the first chunk lands -- so the gap a caller
        experiences becomes time-to-FIRST-byte, not time-to-last.

        Sent without Content-Length, chunked, so the browser plays progressively
        rather than waiting to size the file.
        """
        with request as upstream:
            if upstream.status_code != 200:
                upstream.read()
                return False
            self.send_response(200)
            # The streaming path writes its own headers, so it needs its own
            # CORS. Without this the browser plays the greeting (which is
            # buffered) and goes silent on any provider that streams -- a
            # failure that looks like a voice bug and is a header.
            self._cors()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for chunk in upstream.iter_bytes(4096):
                    if not chunk:
                        continue
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                # The caller interrupted, or hung up mid-sentence. Normal on a
                # phone line and not worth a stack trace.
                log.info("demo.audio_client_gone")
        return True

    def _stt_sarvam(self, audio: bytes, ctype: str, settings: Any) -> None:
        """Saaras. Handles all three languages and identifies which it heard.

        `language_code="unknown"` is the auto-detect setting, and it is the one
        that lets the dropdown stay deleted. Note it is NOT the plugin default
        -- Pipecat's Sarvam integration defaults to `en-IN`, which silently
        turns a multilingual line into an English one.
        """
        try:
            response = HTTP.post(
                SARVAM_STT,
                headers={"api-subscription-key": settings.sarvam_api_key},
                files={"file": ("turn.webm", audio, ctype)},
                data={"model": "saaras:v3", "language_code": "unknown"},
            )
        except httpx.HTTPError as exc:
            return self._json({"error": f"speech-to-text unreachable: {exc}"}, 502)

        if response.status_code != 200:
            return self._json(
                {"error": f"speech-to-text refused ({response.status_code})"}, 502
            )

        body = response.json()
        text = (body.get("transcript") or "").strip()
        detected = to_supported(body.get("language_code")) or detect_language(text)
        self._json({"text": text, "language": detected})

    def _tts_sarvam(self, text: str, language: str, settings: Any) -> None:
        """Bulbul v3. One speaker, three languages, no accent carried across.

        Two things differ from every other provider behind this seam and both
        will trip up the next person:

        1. **The response is JSON, not audio.** `audios[0]` is base64. Reading
           `.content` gets you a JSON document that a browser will try to play.
        2. **`language_code` is required.** There is no auto mode on synthesis
           -- the caller's language is decided upstream by the recogniser and
           handed down, which is why `/api/turn` returns it.

        `temperature` governs prosodic variation, not creativity, and `pace`
        the speed. 0.6 and 1.0 read as a receptionist. There are deliberately
        no named emotions in this API, which for a clinic line is a feature:
        there is no `excited` to leak into a call about an appointment.
        """
        try:
            response = HTTP.post(
                SARVAM_TTS,
                headers={
                    "api-subscription-key": settings.sarvam_api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model": settings.sarvam_tts_model,
                    "language_code": language,
                    "speaker": settings.sarvam_tts_speaker,
                    "pace": 1.0,
                    "temperature": 0.6,
                    "speech_sample_rate": 24000,
                    "output_audio_codec": "mp3",
                    # Sarvam's own text normaliser: numerals and English words
                    # inside an Indic sentence. It is OFF by default, which is
                    # how "9:30" ends up voiced as two numbers with a colon
                    # between them.
                    #
                    # `voice/speech.py` deliberately stops short of spelling
                    # Tamil and Hindi numerals -- sandhi makes a hand-written
                    # table a good way to be confidently wrong in the caller's
                    # own language -- so this is the half that does it, and the
                    # renderer supplies the half it cannot know: whether nine
                    # means morning or night.
                    "enable_preprocessing": True,
                },
            )
        except httpx.HTTPError as exc:
            return self._json({"error": f"voice unreachable: {exc}"}, 502)

        if response.status_code != 200:
            log.warning("demo.sarvam_tts", status=response.status_code)
            return self._json(
                {"error": f"voice refused ({response.status_code})"}, 502
            )

        audios = response.json().get("audios") or []
        if not audios:
            return self._json({"error": "voice returned no audio"}, 502)
        self._send(200, base64.b64decode(audios[0]), "audio/mpeg")

    def _tts_deepgram(self, text: str, settings: Any) -> None:
        """Deepgram Speak (Aura-2).

        English only -- there is no Tamil or Hindi voice in Aura-2 today. When
        the caller is speaking either, this falls through to ElevenLabs if a
        key is present, and to the browser if not. Saying an Indian language in
        an English voice model is the failure that started this: it does not
        sound accented, it sounds like no language at all.
        """
        try:
            request = HTTP.stream(
                "POST",
                DEEPGRAM_TTS,
                params={
                    "model": settings.deepgram_tts_model,
                    "encoding": "mp3",
                },
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": "application/json",
                },
                json={"text": text},
            )
            if not self._stream_audio(request):
                return self._json({"error": "voice refused"}, 502)
        except httpx.HTTPError as exc:
            return self._json({"error": f"voice unreachable: {exc}"}, 502)

    def _tts(self, body: dict[str, Any] | None = None) -> None:
        """The agent's line, in a human voice.

        A proxy rather than a browser-side call, so the API key never reaches
        the page. That matters more than it looks: this page is served on
        localhost today and the first person to put it behind a URL would
        otherwise be publishing the key to every viewer.
        """
        settings = self.desk.settings
        if body is None:
            body = self._read_json()
        text = (body.get("text") or "").strip()
        if not text:
            return self._json({"error": "nothing to say"}, 400)

        language = to_supported(body.get("language")) or "en-IN"

        # Written form in, spoken form out -- the one place it happens, and
        # the last thing before text becomes audio.
        #
        # NOT done in `agent.py`, and the reason is worth the extra hop.
        # `trace.spoken_text` is what the transcript, the audit row and the
        # eval judge read, and the judge finds ungrounded claims by matching
        # DIGITS: `_PHONE` on ten of them, `_clock_claims` on `H:MM`,
        # `_money_claims` on an amount. Normalising upstream of that would not
        # break the scorer loudly -- it would leave it counting zero claims and
        # reporting every call perfectly grounded.
        #
        # So the agent keeps saying "2:15 PM" and only the synthesiser is
        # handed "two fifteen in the afternoon".
        text = for_speech(text, language, timezone=self.desk.tenant.timezone)

        # Deepgram for English, which is what a demo is given in and what
        # Aura-2 is good at. Anything else needs a voice that can form the
        # sounds, so it falls through to ElevenLabs -- and if there is no key
        # for that either, to the browser, which is at least intelligible.
        # Sarvam speaks all three in one voice, so there is nothing to route
        # around and no fallback to reach for.
        if settings.sarvam_api_key:
            return self._tts_sarvam(text, language, settings)

        if settings.deepgram_api_key and language == "en-IN":
            return self._tts_deepgram(text, settings)

        if not settings.elevenlabs_api_key:
            return self._json(
                {"error": f"no voice configured that speaks {language}"}, 503
            )
        voice = (
            os.environ.get("ELEVENLABS_VOICE_" + language.split("-")[0].upper())
            or VOICE_BY_LANGUAGE.get(language)
            or settings.elevenlabs_voice_id
        )

        response = self._speak(text, voice, settings)
        if response is None:
            return self._json({"error": "voice unreachable"}, 502)

        # A per-language voice can be refused when the default would not be:
        # library voices need a paid plan (402 `paid_plan_required`), and the
        # Tamil voice on this account is one. Falling back keeps the call
        # ALIVE and merely accented. Failing here would hand the caller
        # silence, which is worse in every way -- and silence is the one thing
        # a voice product must never produce.
        if response.status_code != 200 and voice != settings.elevenlabs_voice_id:
            log.warning(
                "demo.voice_fallback", voice=voice, status=response.status_code
            )
            response = self._speak(text, settings.elevenlabs_voice_id, settings)
            if response is None:
                return self._json({"error": "voice unreachable"}, 502)

        if response.status_code != 200:
            return self._json(
                {"error": f"voice refused ({response.status_code})"}, 502
            )
        self._send(200, response.content, "audio/mpeg")

    def _speak(self, text: str, voice: str, settings: Any) -> Any:
        try:
            return HTTP.post(
                ELEVENLABS_TTS.format(voice=voice),
                headers={
                    "xi-api-key": settings.elevenlabs_api_key,
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": settings.elevenlabs_model,
                    # Tuned for a receptionist, not a narrator. High stability
                    # reads flat; low stability emotes over a booking
                    # confirmation, which sounds strange rather than warm.
                    "voice_settings": {
                        "stability": 0.42,
                        "similarity_boost": 0.75,
                        "style": 0.32,
                        "use_speaker_boost": True,
                    },
                },
                timeout=45.0,
            )
        except httpx.HTTPError:
            log.warning("demo.voice_unreachable", voice=voice, exc_info=True)
            return None


def main(port: int | None = None) -> int:
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
    try:
        Handler.desk = Desk()
    except ConfigError as exc:
        print(f"Configuration refused: {exc}")
        return 2

    # Railway assigns the port and expects the process to bind it. A hardcoded
    # 8000 there is a container that passes its own health check on a port
    # nothing is routed to, and reports healthy while being unreachable.
    port = port or _int_env("PORT", 8000)

    # 0.0.0.0 in a container, loopback on a laptop. Binding a dev machine to
    # every interface puts an unauthenticated demo with live API keys on
    # whatever café network it is sitting on.
    host = os.environ.get("DEMO_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")  # noqa: S104

    settings = Handler.desk.settings
    bridge_port = port + 1
    # Sarvam first: it is the only recogniser here that hears Tamil at all.
    if settings.sarvam_api_key:
        from voicedesk.demo import bridge

        bridge.start_in_thread(
            settings.sarvam_api_key, settings.sarvam_stt_model, bridge_port, "sarvam"
        )
    elif settings.deepgram_api_key:
        from voicedesk.demo import bridge

        bridge.start_in_thread(
            settings.deepgram_api_key, settings.deepgram_stt_model, bridge_port
        )

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  Voice Desk demo  ->  http://{host}:{port}")
    print(f"  clinic: {Handler.desk.tenant.display_name}")
    print(f"  model:  {Handler.desk.settings.llm_model}")
    print(f"  speech: {settings.speech_provider}")
    print(f"  cors:   {', '.join(ALLOWED_ORIGINS) if ALLOWED_ORIGINS else 'same-origin only'}")
    print(f"  budget: {CALL_BUDGET} calls/hr, {TURN_BUDGET} turns/hr per visitor")
    if settings.deepgram_api_key:
        print(f"  live:   ws://127.0.0.1:{bridge_port}  (streaming transcription)")
    print("\n  ctrl-c to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
