"""Tenant configuration, loaded from disk.

PROJECT.md hard rule 8: **tenant identity lives in config, never in code.** No
clinic name, doctor name or phone number is hardcoded anywhere, and `clinic_id`
is on every row from the first migration.

`.env.example` has pointed `TENANT_CONFIG_PATH` at `./config/tenants` since G0
and that directory did not exist. Meanwhile all 58 eval cases declare
`tenant: meridian`, a tenant defined nowhere. This module and
`config/tenants/meridian.yaml` close both halves.

The loader is strict for the same reason the tool schemas are: a tenant file
with a missing field should fail at startup, where one person sees a clear
error, rather than at 9am on a Monday when `get_clinic_info` raises KeyError
mid-call and the agent has to improvise an answer about consultation fees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args
from uuid import UUID

import yaml

from voicedesk.tools.schemas import GetClinicInfoIn

# Derived from the tool schema rather than restated, so the two cannot drift.
# If someone adds a queryable field to GetClinicInfoIn, every tenant file is
# required to answer it and the loader says which ones do not.
REQUIRED_INFO_FIELDS: frozenset[str] = frozenset(
    get_args(GetClinicInfoIn.model_fields["field"].annotation)
)

_MSISDN = re.compile(r"^(?:\+?91)?[6-9]\d{9}$")

# The schema has no column for any of these and the agent has no grant to read
# one. A tenant file is a place someone might reasonably try to put clinical
# text -- "prep_instructions" is one edit away from "what to do if you feel
# dizzy" -- so the prohibition is enforced here too, at the point of entry.
_CLINICAL_TOKENS = (
    "diagnosis", "symptom", "prescription", "medication", "dosage",
    "treatment", "triage", "lab_result", "test_result",
)


class TenantConfigError(RuntimeError):
    """Always names the file and the field."""


@dataclass(frozen=True)
class Doctor:
    doctor_id: UUID
    full_name: str
    specialty: str
    active: bool = True


@dataclass(frozen=True)
class Tenant:
    """One clinic. Everything the agent may say about itself comes from here."""

    clinic_id: UUID
    slug: str
    display_name: str
    timezone: str
    languages: tuple[str, ...]
    escalation_msisdn: str
    config_version: str
    doctors: tuple[Doctor, ...]
    info: dict[str, str]
    """Flat lookup matching `TenantConfig.get`: a bare field name, plus
    `field.specialty` keys for per-specialty overrides."""

    def active_specialties(self) -> tuple[str, ...]:
        return tuple(sorted({d.specialty for d in self.doctors if d.active}))


def load_tenant(path: Path | str) -> Tenant:
    """Read and validate one tenant file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TenantConfigError(f"{path.name}: unparseable YAML -- {exc}") from exc

    if not isinstance(raw, dict):
        raise TenantConfigError(f"{path.name}: top level must be a mapping")

    def need(key: str) -> Any:
        if key not in raw or raw[key] in (None, ""):
            raise TenantConfigError(f"{path.name}: missing required key '{key}'")
        return raw[key]

    try:
        clinic_id = UUID(str(need("clinic_id")))
    except ValueError as exc:
        raise TenantConfigError(f"{path.name}: clinic_id is not a UUID") from exc

    escalation = str(need("escalation_msisdn"))
    if not _MSISDN.match(escalation):
        raise TenantConfigError(
            f"{path.name}: escalation_msisdn {escalation!r} is not a valid Indian "
            f"mobile. Every uncertain call ends at this number -- a wrong one "
            f"turns the safe default into a dead end."
        )

    languages = tuple(str(x) for x in need("languages"))
    if not languages:
        raise TenantConfigError(f"{path.name}: languages is empty")

    doctors = _load_doctors(path, raw.get("doctors") or [])
    info = _load_info(path, raw.get("info") or {}, raw.get("by_specialty") or {})

    missing = REQUIRED_INFO_FIELDS - set(info)
    if missing:
        raise TenantConfigError(
            f"{path.name}: info is missing {sorted(missing)}. Every field "
            f"get_clinic_info can be asked for must have a grounded answer, or "
            f"the agent has to invent one."
        )

    tenant = Tenant(
        clinic_id=clinic_id,
        slug=str(need("slug")),
        display_name=str(need("display_name")),
        timezone=str(raw.get("timezone") or "Asia/Kolkata"),
        languages=languages,
        escalation_msisdn=escalation,
        config_version=str(need("config_version")),
        doctors=doctors,
        info=info,
    )

    _check_specialties_agree(path, tenant)
    return tenant


def load_tenants(directory: Path | str) -> dict[str, Tenant]:
    """Load every tenant file in a directory, keyed by slug.

    Accepts a str because TENANT_CONFIG_PATH arrives as one from the
    environment, and an AttributeError three frames down is a poor way to learn
    that a path needed wrapping.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise TenantConfigError(
            f"{directory} does not exist. TENANT_CONFIG_PATH must point at a "
            f"directory of tenant YAML files."
        )

    tenants: dict[str, Tenant] = {}
    for path in sorted(directory.glob("*.yaml")):
        tenant = load_tenant(path)
        if tenant.slug in tenants:
            raise TenantConfigError(f"duplicate tenant slug '{tenant.slug}'")
        tenants[tenant.slug] = tenant

    if not tenants:
        raise TenantConfigError(f"no tenant files found in {directory}")
    return tenants


# --------------------------------------------------------------------------


def _load_doctors(path: Path, raw: Any) -> tuple[Doctor, ...]:
    if not isinstance(raw, list) or not raw:
        raise TenantConfigError(f"{path.name}: doctors must be a non-empty list")

    doctors: list[Doctor] = []
    seen: set[UUID] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise TenantConfigError(f"{path.name}: each doctor must be a mapping")
        for key in ("doctor_id", "full_name", "specialty"):
            if not entry.get(key):
                raise TenantConfigError(f"{path.name}: doctor missing '{key}'")
        try:
            doctor_id = UUID(str(entry["doctor_id"]))
        except ValueError as exc:
            raise TenantConfigError(
                f"{path.name}: doctor_id {entry['doctor_id']!r} is not a UUID"
            ) from exc
        if doctor_id in seen:
            raise TenantConfigError(f"{path.name}: duplicate doctor_id {doctor_id}")
        seen.add(doctor_id)
        doctors.append(
            Doctor(
                doctor_id=doctor_id,
                full_name=str(entry["full_name"]),
                specialty=str(entry["specialty"]),
                active=bool(entry.get("active", True)),
            )
        )
    return tuple(doctors)


def _load_info(path: Path, info: Any, by_specialty: Any) -> dict[str, str]:
    """Flatten into the shape `TenantConfig.get` expects.

    `by_specialty: {cardiology: {consult_fee: "900"}}` becomes the key
    `consult_fee.cardiology`, which is what `TenantConfig.get(field, specialty)`
    looks up before falling back to the bare field.
    """
    if not isinstance(info, dict):
        raise TenantConfigError(f"{path.name}: info must be a mapping")

    flat: dict[str, str] = {}
    for key, value in info.items():
        flat[str(key)] = _scalar(path, key, value)

    if by_specialty:
        if not isinstance(by_specialty, dict):
            raise TenantConfigError(f"{path.name}: by_specialty must be a mapping")
        for specialty, overrides in by_specialty.items():
            if not isinstance(overrides, dict):
                raise TenantConfigError(
                    f"{path.name}: by_specialty.{specialty} must be a mapping"
                )
            for key, value in overrides.items():
                # Lowercased on the way in, and TenantConfig.get lowercases on
                # the way out. The model supplies the specialty string from
                # whatever the caller said, so "Cardiology", "cardiology" and
                # "CARDIOLOGY" must all reach the same override -- otherwise the
                # lookup silently falls back to the generic fee and the agent
                # quotes 500 rupees for a 900-rupee consultation.
                flat[f"{key}.{str(specialty).lower()}"] = _scalar(path, key, value)

    for key, value in flat.items():
        lowered = value.lower()
        for token in _CLINICAL_TOKENS:
            if token in lowered:
                raise TenantConfigError(
                    f"{path.name}: info.{key} contains {token!r}. Tenant config "
                    f"carries logistics, never clinical content -- C13/C14 are "
                    f"prohibited and the schema has no column for it. If a "
                    f"clinic genuinely needs this said, stop and escalate."
                )
    return flat


def _scalar(path: Path, key: Any, value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        raise TenantConfigError(f"{path.name}: info.{key} must be a scalar or list")
    return str(value)


def _check_specialties_agree(path: Path, tenant: Tenant) -> None:
    """The advertised specialty list must match who actually works here.

    Otherwise the agent offers Cardiology from `get_clinic_info` and then
    `find_slots` returns nothing, which reads to the caller as the clinic being
    fully booked rather than as never having had a cardiologist.
    """
    advertised = {
        s.strip().lower()
        for s in tenant.info["specialties"].split(",")
        if s.strip()
    }
    staffed = {d.specialty.strip().lower() for d in tenant.doctors if d.active}

    if advertised != staffed:
        raise TenantConfigError(
            f"{path.name}: specialties advertised in info ({sorted(advertised)}) "
            f"do not match the active doctor roster ({sorted(staffed)})"
        )

