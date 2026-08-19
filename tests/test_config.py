"""Configuration is validated at startup, and secrets are demanded late.

`.env.example` documented thirty-odd variables from G0 and nothing read any of
them. Two consequences worth naming, because both were live until 2026-08-19:

  * Rules that existed only in prose -- Vertex AI is blocked, data at rest
    stays in Mumbai, calls must end before the platform drops the connection --
    were unenforceable. A rule in a document is the same category of non-control
    as a rule in a prompt.
  * `TENANT_CONFIG_PATH` pointed at `./config/tenants`, which did not exist,
    while all 58 eval cases declared `tenant: meridian`, a tenant defined
    nowhere.

Nothing here needs a secret. That is the design: CI, the linter and the eval
validator all run on a bare checkout, and a missing Sarvam key fails when the
speech pipeline starts rather than when someone runs the tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO_ROOT

from voicedesk.config import (
    RAILWAY_CONNECTION_LIMIT_SEC,
    REQUIRED_SUPABASE_REGION,
    ConfigError,
    ConsentStore,
    Settings,
    redact,
)
from voicedesk.tenants import (
    REQUIRED_INFO_FIELDS,
    TenantConfigError,
    load_tenant,
    load_tenants,
)
from voicedesk.tools.scheduling import TenantConfig

TENANT_DIR = REPO_ROOT / "config" / "tenants"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from an empty environment.

    Otherwise a developer's real `.env` decides whether the suite passes, and
    the one machine where it fails is CI.
    """
    for name in list(Settings.__dataclass_fields__):
        monkeypatch.delenv(name.upper(), raising=False)
    for name in (
        "DRY_RUN", "KILL_SWITCH", "GOOGLE_GENAI_USE_VERTEXAI", "SUPABASE_REGION",
        "MAX_CALL_DURATION_SEC", "CONSENT_STORE", "LOG_LEVEL", "SARVAM_API_KEY",
        "GOOGLE_AI_API_KEY", "DATABASE_DSN", "RECORDING_RETENTION_DAYS",
        "BOOKING_UNDO_WINDOW_SEC", "AI_DISCLOSURE_ENABLED", "TENANT_CONFIG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


# ==========================================================================
# Defaults fail safe
# ==========================================================================


def test_loads_with_no_environment_at_all() -> None:
    """A bare checkout must not need a .env to import, lint or test."""
    assert Settings.load() is not None


def test_dry_run_defaults_to_true() -> None:
    """Matches ToolContext.dry_run. The default has to be the safe one: a
    forgotten variable should mean 'write nothing', not 'write freely'."""
    assert Settings.load().dry_run is True


def test_ai_disclosure_defaults_to_on() -> None:
    """Unconditional, first turn, every call. Never off by omission."""
    assert Settings.load().ai_disclosure_enabled is True


def test_kill_switch_defaults_to_off_but_is_settable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings.load().kill_switch is False
    monkeypatch.setenv("KILL_SWITCH", "true")
    assert Settings.load().kill_switch is True


# ==========================================================================
# Boolean parsing is strict
# ==========================================================================


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
def test_recognised_true_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("KILL_SWITCH", raw)
    assert Settings.load().kill_switch is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off"])
def test_recognised_false_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("AI_DISCLOSURE_ENABLED", raw)
    assert Settings.load().ai_disclosure_enabled is False


@pytest.mark.parametrize("raw", ["maybe", "y", "2", "disabled", "False;", "0.0"])
def test_an_unrecognised_boolean_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """`bool("false")` is True in Python, which is the classic way a safety
    flag gets inverted by a typo. Every flag here governs something that
    matters, so an unparseable value stops the process rather than being
    interpreted generously in whichever direction Python happens to pick."""
    monkeypatch.setenv("DRY_RUN", raw)
    with pytest.raises(ConfigError, match="not a boolean"):
        Settings.load()


@pytest.mark.parametrize("raw", [" true ", " false "])
def test_surrounding_whitespace_is_tolerated(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A trailing space from a copy-pasted .env line should not stop the
    service. Only genuinely unrecognisable values do."""
    monkeypatch.setenv("KILL_SWITCH", raw)
    assert Settings.load().kill_switch is (raw.strip() == "true")


# ==========================================================================
# Standing constraints, now enforced instead of merely written down
# ==========================================================================


def test_vertex_ai_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standing org-wide block. Previously enforced only by a line in
    CLAUDE.md, which is to say not enforced."""
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    with pytest.raises(ConfigError, match="Vertex AI"):
        Settings.load()


def test_data_residency_region_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """DPDP. Compute may sit outside India -- Railway has no India region --
    but storage may not, and the two are easy to conflate."""
    monkeypatch.setenv("SUPABASE_REGION", "us-east-1")
    with pytest.raises(ConfigError, match="Mumbai"):
        Settings.load()

    monkeypatch.setenv("SUPABASE_REGION", REQUIRED_SUPABASE_REGION)
    assert Settings.load().supabase_region == REQUIRED_SUPABASE_REGION


def test_call_ceiling_must_sit_inside_the_platform_connection_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above the host's limit the platform ends calls instead of the agent --
    mid-sentence, with no wrap-up state and no audit row."""
    monkeypatch.setenv("MAX_CALL_DURATION_SEC", str(RAILWAY_CONNECTION_LIMIT_SEC))
    with pytest.raises(ConfigError, match="connection limit"):
        Settings.load()


def test_the_default_call_ceiling_is_inside_the_limit() -> None:
    assert Settings.load().max_call_duration_sec < RAILWAY_CONNECTION_LIMIT_SEC


def test_retention_cannot_be_raised_past_the_stated_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECORDING_RETENTION_DAYS", "365")
    with pytest.raises(ConfigError, match="privacy decision"):
        Settings.load()


def test_undo_window_cannot_be_made_theoretical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOOKING_UNDO_WINDOW_SEC", "5")
    with pytest.raises(ConfigError, match="undo"):
        Settings.load()


def test_consent_store_is_an_enum_so_it_can_be_swapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DPDP's registered consent-manager framework is live 2026-11-13. The
    store being an interface from day one is what makes that a config change
    rather than a pipeline rewrite."""
    assert Settings.load().consent_store is ConsentStore.LOCAL

    monkeypatch.setenv("CONSENT_STORE", "consent_manager")
    assert Settings.load().consent_store is ConsentStore.CONSENT_MANAGER

    monkeypatch.setenv("CONSENT_STORE", "dropbox")
    with pytest.raises(ConfigError, match="CONSENT_STORE"):
        Settings.load()


# ==========================================================================
# Secrets: demanded late, never logged
# ==========================================================================


@pytest.mark.parametrize(
    ("method", "variable"),
    [
        ("require_speech", "SARVAM_API_KEY"),
        ("require_llm", "GOOGLE_AI_API_KEY"),
        ("require_database", "DATABASE_DSN"),
    ],
)
def test_a_missing_secret_names_itself(method: str, variable: str) -> None:
    """The error has to say which variable and what breaks without it. A
    KeyError three frames deep costs an hour."""
    settings = Settings.load()
    with pytest.raises(ConfigError, match=variable):
        getattr(settings, method)()


def test_a_present_secret_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARVAM_API_KEY", "sk-not-a-real-key")
    assert Settings.load().require_speech() == "sk-not-a-real-key"


def test_secrets_never_appear_in_the_loggable_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """structlog is wired through the tool path. The surest way to leak a key
    is to log the config object while debugging something else."""
    monkeypatch.setenv("SARVAM_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "AIza-super-secret-value")

    rendered = str(Settings.load().loggable())
    assert "super-secret-value" not in rendered
    assert "AIza" not in rendered


def test_repr_does_not_carry_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A traceback prints repr. That is not the moment to find out."""
    monkeypatch.setenv("SARVAM_API_KEY", "sk-super-secret-value")
    assert "super-secret-value" not in repr(Settings.load())


@pytest.mark.parametrize(
    "name", ["SARVAM_API_KEY", "PLIVO_AUTH_TOKEN", "DATABASE_DSN", "SUPABASE_SERVICE_KEY"]
)
def test_secret_shaped_names_are_redacted(name: str) -> None:
    assert "hunter2" not in redact(name, "hunter2")


def test_non_secret_values_stay_readable() -> None:
    """Redacting everything makes the log useless and gets redaction removed."""
    assert redact("GEMINI_MODEL", "gemini-2.5-flash") == "gemini-2.5-flash"


# ==========================================================================
# The tenant the eval suite already depends on
# ==========================================================================


def test_the_demo_tenant_directory_exists() -> None:
    """`.env.example` has pointed TENANT_CONFIG_PATH here since G0."""
    assert TENANT_DIR.is_dir(), f"{TENANT_DIR} is missing"


def test_meridian_loads() -> None:
    """All 58 eval cases declare `tenant: meridian`."""
    tenants = load_tenants(TENANT_DIR)
    assert "meridian" in tenants


def test_every_eval_case_names_a_tenant_that_exists() -> None:
    """The check that would have caught a whole suite pointing at nothing."""
    import yaml

    tenants = load_tenants(TENANT_DIR)
    cases = sorted((REPO_ROOT / "evals" / "cases").rglob("*.yaml"))
    assert cases, "no eval cases found"

    missing = set()
    for path in cases:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        slug = (raw or {}).get("tenant", "meridian")
        if slug not in tenants:
            missing.add(slug)

    assert not missing, f"eval cases reference undefined tenants: {sorted(missing)}"


def test_every_queryable_field_has_a_grounded_answer() -> None:
    """REQUIRED_INFO_FIELDS is derived from GetClinicInfoIn, so adding a field
    to the tool makes every tenant file fail until it answers it. Otherwise the
    agent is asked something it has no source for, and grounding failures are
    exactly how a voice agent invents a consultation fee."""
    tenant = load_tenants(TENANT_DIR)["meridian"]
    assert REQUIRED_INFO_FIELDS <= set(tenant.info)


def test_advertised_specialties_match_the_roster() -> None:
    """Otherwise get_clinic_info offers Cardiology, find_slots returns nothing,
    and the caller hears 'fully booked' rather than 'we have no cardiologist'."""
    tenant = load_tenants(TENANT_DIR)["meridian"]
    advertised = {s.strip() for s in tenant.info["specialties"].split(",")}
    assert advertised == set(tenant.active_specialties())


def test_the_deliberate_name_collisions_survive() -> None:
    """Several eval cases are built on ambiguous names. A tidy-up that gives
    every doctor a distinct first name silently disarms them."""
    tenant = load_tenants(TENANT_DIR)["meridian"]
    names = [d.full_name for d in tenant.doctors]

    anithas = [n for n in names if "Anitha" in n]
    assert len(anithas) >= 3, "ambiguous-009 needs a partial name matching several"

    assert "Dr. Thirumalai Sekar" in names
    assert "Dr. Thirumurugan Sekaran" in names, (
        "codeswitch-009 needs the fuzzy-match trap: ASR mangles the first name "
        "into something a matcher scores against the second"
    )


def test_specialty_lookup_is_case_insensitive() -> None:
    """The model passes whatever the caller said. A case-sensitive key means
    'cardiology' silently falls back to the generic fee, and the agent quotes
    500 rupees for a 900-rupee consultation."""
    tenant = load_tenants(TENANT_DIR)["meridian"]
    config = TenantConfig.from_tenant(tenant)

    keys = {config.get("consult_fee", s)[1] for s in ("Cardiology", "cardiology", "CARDIOLOGY")}
    assert keys == {"consult_fee.cardiology"}


def test_unknown_specialty_falls_back_to_the_generic_answer() -> None:
    tenant = load_tenants(TENANT_DIR)["meridian"]
    config = TenantConfig.from_tenant(tenant)
    _, source = config.get("consult_fee", "Neurology")
    assert source == "consult_fee"


def test_escalation_number_is_reachable_shaped() -> None:
    """Every uncertain call ends at this number. C10 makes transfer the safe
    default, which a malformed number turns into a dead end."""
    tenant = load_tenants(TENANT_DIR)["meridian"]
    assert tenant.escalation_msisdn.startswith("+91")


# ==========================================================================
# The loader refuses bad tenant files
# ==========================================================================


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "broken.yaml"
    path.write_text(body, encoding="utf-8")
    return path


VALID = """
clinic_id: d0c70000-0000-4000-8000-0000000000ff
slug: test
display_name: Test Clinic
config_version: "1"
languages: [en-IN]
escalation_msisdn: "+919000000000"
info:
  opd_hours: "9 to 5"
  address: "somewhere"
  consult_fee: "100"
  specialties: "General Medicine"
  doctors: "one"
  prep_instructions: "bring id"
  languages: "English"
doctors:
  - doctor_id: d0c70001-0000-4000-8000-0000000000ff
    full_name: Dr. Test
    specialty: General Medicine
"""


def test_the_control_file_is_valid(tmp_path: Path) -> None:
    """If this ever fails, every negative test below is passing for the wrong
    reason."""
    assert load_tenant(_write(tmp_path, VALID)).slug == "test"


def test_missing_required_key_is_refused(tmp_path: Path) -> None:
    body = VALID.replace('display_name: Test Clinic\n', "")
    with pytest.raises(TenantConfigError, match="display_name"):
        load_tenant(_write(tmp_path, body))


def test_malformed_escalation_number_is_refused(tmp_path: Path) -> None:
    body = VALID.replace('"+919000000000"', '"12345"')
    with pytest.raises(TenantConfigError, match="escalation_msisdn"):
        load_tenant(_write(tmp_path, body))


def test_missing_info_field_is_refused(tmp_path: Path) -> None:
    body = VALID.replace('  consult_fee: "100"\n', "")
    with pytest.raises(TenantConfigError, match="consult_fee"):
        load_tenant(_write(tmp_path, body))


def test_specialty_mismatch_is_refused(tmp_path: Path) -> None:
    body = VALID.replace('specialties: "General Medicine"', 'specialties: "Cardiology"')
    with pytest.raises(TenantConfigError, match="do not match"):
        load_tenant(_write(tmp_path, body))


def test_clinical_content_in_tenant_config_is_refused(tmp_path: Path) -> None:
    """`prep_instructions` is one edit away from 'what to do if you feel
    dizzy'. C13/C14 are prohibited and the schema has no column for it, so the
    prohibition is enforced at the point of entry too."""
    body = VALID.replace(
        'prep_instructions: "bring id"',
        'prep_instructions: "stop your diabetes medication the night before"',
    )
    with pytest.raises(TenantConfigError, match="clinical"):
        load_tenant(_write(tmp_path, body))


def test_duplicate_doctor_id_is_refused(tmp_path: Path) -> None:
    body = VALID + """  - doctor_id: d0c70001-0000-4000-8000-0000000000ff
    full_name: Dr. Clone
    specialty: General Medicine
"""
    with pytest.raises(TenantConfigError, match="duplicate doctor_id"):
        load_tenant(_write(tmp_path, body))


def test_a_missing_tenant_directory_says_so(tmp_path: Path) -> None:
    with pytest.raises(TenantConfigError, match="does not exist"):
        load_tenants(tmp_path / "nope")


def test_an_empty_tenant_directory_is_refused(tmp_path: Path) -> None:
    """Silently loading zero tenants means the first call has nowhere to route."""
    with pytest.raises(TenantConfigError, match="no tenant files"):
        load_tenants(tmp_path)
