"""Environment configuration, validated at startup.

`.env.example` has documented thirty-odd variables since G0 and nothing read
any of them. This is the module that does, and it is deliberately strict: a
misconfigured voice agent fails to boot rather than answering a call in a state
nobody intended.

Several rules that previously lived only in prose are enforced here, because a
rule in a document is the same category of non-control as a rule in a prompt:

  * Vertex AI is a standing org-wide block -> refuse to start if it is on.
  * Data at rest stays in Supabase Mumbai -> refuse any other region.
  * Railway drops connections at 15 minutes -> refuse a call ceiling above it.
  * Consent must be swappable to a registered consent manager (live
    2026-11-13) -> the store is an enum, not a hardcoded path.

**No secret is ever required merely to import this module.** Tests, the eval
validator and CI all run on a bare checkout with no `.env` at all. Secrets are
demanded at the point of use, by the `require_*` methods, so that a missing
Sarvam key fails when the speech pipeline starts and not when someone runs the
linter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Railway terminates a connection at 15 minutes. A call ceiling at or above
# that is not a ceiling -- the platform decides instead, mid-sentence.
# See PROJECT.md 2.4.
RAILWAY_CONNECTION_LIMIT_SEC = 900

# DPDP residency. PROJECT.md 2.3 and CLAUDE.md both call this non-negotiable.
REQUIRED_SUPABASE_REGION = "ap-south-1"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_DSN", "_AUTH_ID")


class ConfigError(RuntimeError):
    """Startup refusal. Always names the variable and what it is for."""


class LlmProvider(str, Enum):
    """Which reasoning provider to use.

    A seam, not a preference. G7 requires a provider fallback, and the first
    time it was needed was not a planned drill: Google AI Studio refused the
    project with a 403 that no configuration could fix.
    """

    GOOGLE = "google"
    OPENROUTER = "openrouter"


class ConsentStore(str, Enum):
    LOCAL = "local"
    CONSENT_MANAGER = "consent_manager"
    """DPDP's registered consent-manager framework goes live 2026-11-13. The
    store is an interface from day one so that swap is a config change."""


def load_dotenv_once(path: Path | None = None) -> None:
    """Read `.env` into the environment, without overriding what is already set.

    `python-dotenv` has been a declared dependency since G0, `.env.example`
    says "copy to .env and fill in", and nothing called it. A key placed in
    `.env` was silently ignored and the shell reported it as unset -- which is
    the worst version of this bug, because the user has done the right thing
    and is told they have not.

    Real environment variables win. A value exported in a shell or injected by
    Railway must beat a stale line in a local file, or debugging a deployment
    means wondering which of the two is in force.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        return

    candidate = path or Path.cwd() / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=False)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bool(name: str, default: bool) -> bool:
    """Strict. An unrecognised value is an error, not a guess.

    `bool("false")` is True in Python, which is the classic way a safety flag
    gets inverted by a typo. Every flag here governs something that matters --
    dry-run, the kill switch, AI disclosure -- so an unparseable value stops
    the process instead of being interpreted generously.
    """
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(
        f"{name}={raw!r} is not a boolean. Use one of: "
        f"{', '.join(sorted(_TRUE | _FALSE))}"
    )


def _int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name}={value} must be at least {minimum}")
    return value


def redact(name: str, value: str | None) -> str:
    """How a variable may appear in a log line.

    structlog is wired into the tool path, and the surest way to leak an API
    key is to log the config object while debugging something else.
    """
    if value is None:
        return "<unset>"
    if any(name.upper().endswith(s) for s in _SECRET_SUFFIXES):
        return f"<set:{len(value)} chars>"
    return value


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the environment, taken once at startup."""

    # -- safety rails (G7) ------------------------------------------------
    dry_run: bool = True
    kill_switch: bool = False
    max_cost_per_call_inr: int = 25
    max_cost_per_day_inr: int = 2000
    max_turns_per_call: int = 40
    max_call_duration_sec: int = 420
    booking_undo_window_sec: int = 900

    # -- compliance -------------------------------------------------------
    recording_retention_days: int = 90
    consent_store: ConsentStore = ConsentStore.LOCAL
    ai_disclosure_enabled: bool = True

    # -- speech -----------------------------------------------------------
    sarvam_api_key: str | None = None
    sarvam_stt_model: str = "saaras:v3"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_default_language: str = "ta-IN"

    # -- reasoning --------------------------------------------------------
    llm_provider: LlmProvider = LlmProvider.GOOGLE
    google_ai_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    use_vertex_ai: bool = False
    openrouter_api_key: str | None = None
    openrouter_model: str = "deepseek/deepseek-chat"

    # -- data -------------------------------------------------------------
    database_dsn: str | None = None
    supabase_region: str = REQUIRED_SUPABASE_REGION

    # -- tenancy ----------------------------------------------------------
    tenant_config_path: Path = field(default_factory=lambda: Path("./config/tenants"))
    default_clinic_id: str | None = None

    # -- observability ----------------------------------------------------
    log_level: str = "INFO"

    # ---------------------------------------------------------------------

    @classmethod
    def load(cls, *, env_file: Path | None = None) -> Settings:
        """Read and validate. Raises ConfigError with the offending variable."""
        load_dotenv_once(env_file)
        settings = cls(
            dry_run=_bool("DRY_RUN", True),
            kill_switch=_bool("KILL_SWITCH", False),
            max_cost_per_call_inr=_int("MAX_COST_PER_CALL_INR", 25),
            max_cost_per_day_inr=_int("MAX_COST_PER_DAY_INR", 2000),
            max_turns_per_call=_int("MAX_TURNS_PER_CALL", 40),
            max_call_duration_sec=_int("MAX_CALL_DURATION_SEC", 420),
            booking_undo_window_sec=_int("BOOKING_UNDO_WINDOW_SEC", 900),
            recording_retention_days=_int("RECORDING_RETENTION_DAYS", 90),
            consent_store=_consent_store(),
            ai_disclosure_enabled=_bool("AI_DISCLOSURE_ENABLED", True),
            sarvam_api_key=_env("SARVAM_API_KEY"),
            sarvam_stt_model=_env("SARVAM_STT_MODEL") or "saaras:v3",
            sarvam_tts_model=_env("SARVAM_TTS_MODEL") or "bulbul:v3",
            sarvam_default_language=_env("SARVAM_DEFAULT_LANGUAGE") or "ta-IN",
            llm_provider=_llm_provider(),
            google_ai_api_key=_env("GOOGLE_AI_API_KEY"),
            openrouter_api_key=_env("OPENROUTER_API_KEY"),
            openrouter_model=_env("OPENROUTER_MODEL") or "deepseek/deepseek-chat",
            gemini_model=_env("GEMINI_MODEL") or "gemini-3.6-flash",
            use_vertex_ai=_bool("GOOGLE_GENAI_USE_VERTEXAI", False),
            database_dsn=_env("DATABASE_DSN") or _env("SUPABASE_DB_DSN"),
            supabase_region=_env("SUPABASE_REGION") or REQUIRED_SUPABASE_REGION,
            tenant_config_path=Path(_env("TENANT_CONFIG_PATH") or "./config/tenants"),
            default_clinic_id=_env("DEFAULT_CLINIC_ID"),
            log_level=(_env("LOG_LEVEL") or "INFO").upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Invariants that hold regardless of which secrets are present."""
        if self.use_vertex_ai and self.llm_provider is LlmProvider.GOOGLE:
            raise ConfigError(
                "GOOGLE_GENAI_USE_VERTEXAI must be false. Vertex AI is a "
                "standing org-wide block; use Google AI Studio. See CLAUDE.md "
                "and the vault tech_stack note."
            )

        if self.supabase_region != REQUIRED_SUPABASE_REGION:
            raise ConfigError(
                f"SUPABASE_REGION={self.supabase_region!r} is not "
                f"{REQUIRED_SUPABASE_REGION!r}. Data at rest stays in Mumbai "
                f"under DPDP residency -- this is not a tuning knob. Compute "
                f"may sit outside India (Railway has no India region); storage "
                f"may not."
            )

        if self.max_call_duration_sec >= RAILWAY_CONNECTION_LIMIT_SEC:
            raise ConfigError(
                f"MAX_CALL_DURATION_SEC={self.max_call_duration_sec} is at or "
                f"above the {RAILWAY_CONNECTION_LIMIT_SEC}s platform "
                f"connection limit, so the host would end calls instead of the "
                f"agent -- mid-sentence, with no wrap-up and no audit row."
            )

        if self.booking_undo_window_sec < 60:
            raise ConfigError(
                "BOOKING_UNDO_WINDOW_SEC below 60 makes the undo window "
                "theoretical. G3 requires undo to be usable, not merely present."
            )

        if self.recording_retention_days > 90:
            raise ConfigError(
                f"RECORDING_RETENTION_DAYS={self.recording_retention_days} "
                f"exceeds the 90-day retention posture recorded in PROJECT.md "
                f"2.3. Raising it is a privacy decision, not a config tweak."
            )

        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(f"LOG_LEVEL={self.log_level!r} is not a log level")

    # -- secrets, demanded at the point of use ----------------------------

    def require_speech(self) -> str:
        return self._require("SARVAM_API_KEY", self.sarvam_api_key, "the speech pipeline")

    def require_llm(self) -> str:
        if self.llm_provider is LlmProvider.OPENROUTER:
            return self._require(
                "OPENROUTER_API_KEY", self.openrouter_api_key, "the reasoning step"
            )
        return self._require(
            "GOOGLE_AI_API_KEY", self.google_ai_api_key, "the reasoning step"
        )

    @property
    def llm_model(self) -> str:
        """The model id in force, whichever provider is selected."""
        if self.llm_provider is LlmProvider.OPENROUTER:
            return self.openrouter_model
        return self.gemini_model

    def require_database(self) -> str:
        return self._require(
            "DATABASE_DSN", self.database_dsn, "the scheduling adapter"
        )

    @staticmethod
    def _require(name: str, value: str | None, purpose: str) -> str:
        if not value:
            raise ConfigError(
                f"{name} is not set, and {purpose} cannot start without it. "
                f"Copy .env.example to .env and fill it in. Never commit .env."
            )
        return value

    # -- logging safety ---------------------------------------------------

    def loggable(self) -> dict[str, str]:
        """A dict safe to hand to structlog. Secrets appear as lengths only."""
        return {
            "DRY_RUN": str(self.dry_run),
            "KILL_SWITCH": str(self.kill_switch),
            "SARVAM_API_KEY": redact("SARVAM_API_KEY", self.sarvam_api_key),
            "LLM_PROVIDER": self.llm_provider.value,
            "GOOGLE_AI_API_KEY": redact("GOOGLE_AI_API_KEY", self.google_ai_api_key),
            "OPENROUTER_API_KEY": redact(
                "OPENROUTER_API_KEY", self.openrouter_api_key
            ),
            "DATABASE_DSN": redact("DATABASE_DSN", self.database_dsn),
            "GEMINI_MODEL": self.gemini_model,
            "SARVAM_STT_MODEL": self.sarvam_stt_model,
            "SUPABASE_REGION": self.supabase_region,
            "CONSENT_STORE": self.consent_store.value,
            "MAX_CALL_DURATION_SEC": str(self.max_call_duration_sec),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        """Never let a stray print or traceback carry a key."""
        return f"Settings({self.loggable()})"


def _llm_provider() -> LlmProvider:
    raw = _env("LLM_PROVIDER") or LlmProvider.GOOGLE.value
    try:
        return LlmProvider(raw.lower())
    except ValueError as exc:
        allowed = ", ".join(p.value for p in LlmProvider)
        raise ConfigError(f"LLM_PROVIDER={raw!r} is not one of: {allowed}") from exc


def _consent_store() -> ConsentStore:
    raw = _env("CONSENT_STORE") or ConsentStore.LOCAL.value
    try:
        return ConsentStore(raw)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in ConsentStore)
        raise ConfigError(f"CONSENT_STORE={raw!r} is not one of: {allowed}") from exc
