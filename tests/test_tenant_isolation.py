"""No query may cross `clinic_id`.

0001_init.sql enabled row-level security on ten tables and defined no policies.
Postgres default-denies in that state, so the effect was not "cross-tenant reads
return empty" as the comment claimed — it was that *every* read returned empty
and the grants were dead. This suite exists so that neither half of that
regression can come back quietly: the policies must be present and correctly
shaped, and the adapter must scope every statement itself regardless.

Two mechanisms, tested separately, because tenant isolation is the property in
this system that is most expensive to get wrong:

  1. RLS policies confine the transaction (`0002_rls_policies.sql`).
  2. Every adapter statement names `clinic_id` in its own WHERE or CHECK.

A test that a real cross-tenant SELECT returns zero rows needs a live database
and belongs in the integration suite. Everything here runs on a bare checkout,
which is what makes it a gate rather than a nice-to-have.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import MIGRATIONS, migration_sql

from voicedesk.adapters.postgres import EXPECTED_ROLE, isolation_problems

ADAPTER_SRC = (
    Path(__file__).parent.parent / "src" / "voicedesk" / "adapters" / "postgres.py"
)

# Tables carrying a clinic_id column. `clinics` is excluded: it keys on `id`,
# and is asserted separately.
TENANT_TABLES = (
    "doctors", "opd_slots", "patients", "calls", "call_state_transitions",
    "call_turns", "consent_artefacts", "appointments", "agent_actions",
)

RLS_TABLES = TENANT_TABLES + ("clinics",)


# ==========================================================================
# The policies exist and are correctly shaped
# ==========================================================================


def test_every_rls_enabled_table_has_at_least_one_policy() -> None:
    """The exact defect 0002 was written to fix. RLS on with no policy is not
    'locked down', it is broken — the grants become unreachable."""
    sql = migration_sql()
    enabled = set(
        re.findall(
            r"alter table\s+(\w+)\s+enable row level security", sql, re.IGNORECASE
        )
    )
    assert enabled, "no table has RLS enabled — 0001 regressed"

    with_policy = set(re.findall(r"create policy\s+\w+\s+on\s+(\w+)", sql, re.IGNORECASE))

    missing = enabled - with_policy
    assert not missing, (
        f"RLS is enabled with no policy on: {sorted(missing)}. Postgres "
        f"default-denies, so every statement against these tables returns "
        f"nothing for any non-owner role."
    )


@pytest.mark.parametrize("table", RLS_TABLES)
def test_table_has_rls_enabled(table: str) -> None:
    sql = migration_sql()
    assert re.search(
        rf"alter table\s+{table}\s+enable row level security", sql, re.IGNORECASE
    ), f"{table} does not have RLS enabled"


def test_every_insert_policy_has_a_with_check_clause() -> None:
    """An insert policy without `with check` does not constrain anything. It is
    the direction people forget: reads are obviously dangerous, but an insert
    that lands in another tenant is the same breach arriving backwards."""
    sql = migration_sql()
    for policy in re.findall(
        r"create policy\s+(\w+)[^;]*?for insert[^;]*;", sql, re.IGNORECASE | re.DOTALL
    ):
        block = re.search(
            rf"create policy\s+{policy}\b[^;]*;", sql, re.IGNORECASE | re.DOTALL
        )
        assert block is not None
        assert "with check" in block.group().lower(), (
            f"insert policy '{policy}' has no with-check clause"
        )


def test_every_update_policy_has_both_using_and_with_check() -> None:
    """`using` picks the rows you may target; `with check` picks what they may
    become. With only the first, an update can move a row to another clinic."""
    sql = migration_sql()
    for policy in re.findall(
        r"create policy\s+(\w+)[^;]*?for update[^;]*;", sql, re.IGNORECASE | re.DOTALL
    ):
        block = re.search(
            rf"create policy\s+{policy}\b[^;]*;", sql, re.IGNORECASE | re.DOTALL
        )
        assert block is not None
        text = block.group().lower()
        assert "using" in text, f"update policy '{policy}' has no using clause"
        assert "with check" in text, f"update policy '{policy}' has no with-check clause"


def test_every_policy_compares_against_the_tenant_function() -> None:
    """No policy may hardcode a tenant or use anything but the session GUC."""
    sql = migration_sql()
    policies = re.findall(r"create policy\s+(\w+)\b[^;]*;", sql, re.IGNORECASE | re.DOTALL)
    assert policies, "no policies found at all"

    for policy in policies:
        block = re.search(
            rf"create policy\s+{policy}\b[^;]*;", sql, re.IGNORECASE | re.DOTALL
        )
        assert block is not None
        assert "app_clinic_id()" in block.group(), (
            f"policy '{policy}' does not scope on app_clinic_id()"
        )


def test_tenant_function_fails_closed_when_the_guc_is_unset() -> None:
    """`current_setting(..., true)` returns NULL rather than raising, and
    `clinic_id = NULL` is NULL, not true. So a code path that forgets to set the
    tenant reads nothing instead of reading everything. Losing the second
    argument would silently turn a deny into an exception — or worse, someone
    'fixing' the exception with a default.
    """
    sql = migration_sql()
    fn = re.search(
        r"create or replace function app_clinic_id\(\).*?\$\$(.*?)\$\$",
        sql,
        re.DOTALL | re.I,
    )
    assert fn is not None, "app_clinic_id() is not defined"
    body = fn.group()
    assert "current_setting('app.clinic_id', true)" in body, (
        "app_clinic_id() must use the two-argument current_setting so an unset "
        "tenant is NULL rather than an error"
    )
    assert "nullif" in body.lower(), "an empty-string GUC must also become NULL"


def test_tenant_function_is_not_security_definer() -> None:
    """SECURITY DEFINER would run the function as its owner, which is exactly
    the privilege escalation the tenant boundary is meant to prevent."""
    sql = migration_sql()
    fn = re.search(
        r"create or replace function app_clinic_id\(\).*?\$\$.*?\$\$", sql, re.DOTALL | re.I
    )
    assert fn is not None
    assert "security definer" not in fn.group().lower()


def test_no_policy_grants_the_agent_more_than_one_clinic() -> None:
    """Guards against the shortcut of `using (true)` while debugging."""
    sql = migration_sql()
    for bad in (r"using\s*\(\s*true\s*\)", r"with check\s*\(\s*true\s*\)"):
        assert not re.search(bad, sql, re.IGNORECASE), (
            f"a policy uses an unconditional predicate: {bad}"
        )


# ==========================================================================
# The adapter scopes every statement itself
# ==========================================================================


def _statements_touching_tenant_tables(source: str) -> list[str]:
    out = []
    for block in re.findall(r'"""(.*?)"""', source, re.DOTALL):
        if not re.search(r"\b(select|insert into|update)\b", block, re.IGNORECASE):
            continue
        if any(re.search(rf"\b{t}\b", block) for t in TENANT_TABLES):
            out.append(block)
    return out


def test_adapter_has_statements_to_check() -> None:
    """If the extraction ever returns nothing, every test below would pass
    vacuously. That is how a broken check reports a clean suite."""
    statements = _statements_touching_tenant_tables(
        ADAPTER_SRC.read_text(encoding="utf-8")
    )
    assert len(statements) >= 8, (
        f"expected the adapter's statements to be found; got {len(statements)}"
    )


def test_every_adapter_statement_names_clinic_id() -> None:
    """RLS already confines the transaction. This is the second mechanism: a
    policy lives in a migration, a WHERE clause lives next to the query, and
    the property should survive losing either one."""
    source = ADAPTER_SRC.read_text(encoding="utf-8")
    offenders = [
        stmt.strip().splitlines()[0].strip()
        for stmt in _statements_touching_tenant_tables(source)
        if "clinic_id" not in stmt
    ]
    assert not offenders, (
        "these adapter statements touch a tenant table without naming "
        f"clinic_id: {offenders}"
    )


def test_adapter_builds_no_sql_by_string_formatting() -> None:
    """Every value is a bound parameter. No f-string, no concatenation, no
    `.format()` anywhere near a statement."""
    source = ADAPTER_SRC.read_text(encoding="utf-8")
    assert not re.search(r'f"""', source), "f-string SQL block in the adapter"
    assert not re.search(r'"""[^"]*\{\w+\}[^"]*"""', source, re.DOTALL), (
        "interpolation placeholder inside a SQL block"
    )
    for bad in (r'"\s*\+\s*\w+\s*\+\s*"', r"\.format\("):
        assert not re.search(bad, source), f"SQL assembled by concatenation: {bad}"


def test_adapter_sets_the_tenant_transaction_locally() -> None:
    """Session-scoped would leak one clinic's id into the next call handed the
    same pooled connection. The third argument to set_config is what makes it
    transaction-local, and it is easy to drop."""
    source = ADAPTER_SRC.read_text(encoding="utf-8")
    assert re.search(
        r"set_config\(\s*'app\.clinic_id'\s*,\s*\$1\s*,\s*true\s*\)", source
    ), "the adapter must set app.clinic_id transaction-locally"


def test_tenant_scope_is_acquired_inside_a_transaction() -> None:
    """set_config(..., true) outside a transaction is a no-op that fails open."""
    source = ADAPTER_SRC.read_text(encoding="utf-8")
    guard = re.search(
        r"async def _tenant_tx.*?yield conn", source, re.DOTALL
    )
    assert guard is not None, "_tenant_tx not found"
    assert "conn.transaction()" in guard.group(), (
        "_tenant_tx must open a transaction before setting the tenant GUC"
    )


def test_every_public_adapter_method_takes_clinic_id_first() -> None:
    """A method that can be called without naming a tenant is a method that
    will be."""
    source = ADAPTER_SRC.read_text(encoding="utf-8")
    methods = re.findall(r"async def ([a-z]\w*)\(\s*self,\s*([^,)]*)", source)
    checked = 0
    for name, first_arg in methods:
        if name.startswith("_"):
            continue
        checked += 1
        assert "clinic_id" in first_arg, (
            f"{name}() does not take clinic_id as its first argument"
        )
    assert checked >= 7, f"expected the adapter's public methods; found {checked}"


# ==========================================================================
# RLS is void if the service connects as the wrong role
# ==========================================================================


def test_agent_role_is_the_expected_one() -> None:
    assert EXPECTED_ROLE == "voicedesk_agent"
    assert re.search(
        rf"create role {EXPECTED_ROLE}\b", migration_sql(), re.IGNORECASE
    ), "the agent role is not created by any migration"


def test_correct_role_passes_the_isolation_check() -> None:
    assert isolation_problems(
        EXPECTED_ROLE, is_super=False, bypasses_rls=False
    ) == []


@pytest.mark.parametrize(
    ("role", "is_super", "bypasses", "expected_fragment"),
    [
        ("postgres", False, False, "expected"),
        ("postgres", True, False, "superuser"),
        (EXPECTED_ROLE, True, False, "superuser"),
        (EXPECTED_ROLE, False, True, "BYPASSRLS"),
        ("supabase_admin", True, True, "expected"),
    ],
)
def test_a_role_that_escapes_rls_is_refused(
    role: str, is_super: bool, bypasses: bool, expected_fragment: str
) -> None:
    """A table's owner bypasses RLS, and so does any BYPASSRLS role. Connecting
    as either makes every policy in 0002 inert — and the failure is silent,
    which is the worst kind. Startup must fail instead."""
    problems = isolation_problems(role, is_super=is_super, bypasses_rls=bypasses)
    assert problems, f"role {role} should have been refused"
    assert any(expected_fragment in p for p in problems), problems


def test_force_rls_is_a_documented_decision_not_an_oversight() -> None:
    """FORCE ROW LEVEL SECURITY would apply policies to the owner too. It is
    deliberately not used, because migrations and the staff dashboard run as
    the owner — so the adapter's role assertion carries that weight instead.
    If the reasoning ever leaves the migration, the tradeoff becomes invisible.
    """
    sql = (MIGRATIONS / "0002_rls_policies.sql").read_text(encoding="utf-8")
    assert "force row level security" in sql.lower(), (
        "0002 must explain the FORCE RLS decision, even if only to reject it"
    )


# ==========================================================================
# Grants the code actually needs
# ==========================================================================


@pytest.mark.parametrize(
    ("table", "privilege", "why"),
    [
        ("agent_actions", "select", "AuditSink.find_replay reads it for idempotency"),
        ("patients", "insert", "confirm_booking must record a first-time caller"),
        ("calls", "select", "the session reads its own call row"),
        ("consent_artefacts", "select", "a pre-write validator confirms consent exists"),
    ],
)
def test_grant_exists_for_a_privilege_the_code_depends_on(
    table: str, privilege: str, why: str
) -> None:
    """Each of these was missing from 0001 and would have failed at runtime,
    not at deploy — the worst place to find a permission error."""
    sql = migration_sql()
    pattern = rf"grant[^;]*\b{privilege}\b[^;]*\b{table}\b[^;]*;"
    assert re.search(pattern, sql, re.IGNORECASE | re.DOTALL), (
        f"no {privilege.upper()} grant on {table}: {why}"
    )


def test_bigserial_tables_have_sequence_usage_granted() -> None:
    """Without USAGE on the sequence, every insert into an append-only audit
    table fails on permission."""
    sql = migration_sql()
    for table in ("call_state_transitions", "call_turns", "agent_actions"):
        assert re.search(
            rf"grant usage on sequence {table}_id_seq", sql, re.IGNORECASE
        ), f"{table} inserts will fail without sequence usage"


def test_cancelled_at_column_exists_for_the_cancel_tool() -> None:
    """CancelOut.cancelled_at had no column to come from in 0001."""
    sql = migration_sql()
    assert re.search(r"\bcancelled_at\b", sql), "appointments.cancelled_at missing"
    assert re.search(
        r"grant update\s*\([^)]*cancelled_at[^)]*\)\s*\n?\s*on appointments",
        sql,
        re.IGNORECASE,
    ), "cancelled_at is not writable by the agent role"
