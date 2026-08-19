# Tests

**G6 — deterministic validators. The LLM reasons. The system validates.**

No validator lives only in a prompt. Each is independently runnable and tested.

## Modules

| File | Asserts |
|---|---|
| `test_config.py` | ✅ **Done.** Startup validation: strict booleans, Vertex AI refused, Mumbai region pinned, call ceiling inside the platform limit, secrets never logged, and the demo tenant every eval case references |
| `test_prohibited.py` | The prohibited row of the risk register (C12–C17) is unreachable: no outbound code path, no DELETE grant, no clinical table grant, no payment tool. **Runs in CI as a blocking job.** |
| `test_slot_validity.py` | ✅ **Done** — folded into `test_booking_rules.py`. Slot exists, is in the future, belongs to the tenant, specialty matching is case-insensitive |
| `test_double_booking.py` | ✅ **Done** — in `test_booking_rules.py`. Two confirmations cannot both win; the real adapter is asserted to let the constraint decide rather than pre-checking |
| `test_identity.py` | ✅ **Done.** Identity is server-side, not a model-supplied argument; `find_appointments` accepts no msisdn; writes bound to the verified caller in SQL. The 3-attempt cap belongs to the state machine and is still to do |
| `test_consent.py` | Consent artefact exists and is keyed to the call trace ID before any write |
| `test_clinical_guard.py` | ✅ **Done.** Output-side classifier catches advice, triage (both directions), symptom interpretation, results, dosage — paraphrases not keywords, ta/hi/en parity, grounded config exempt. **Runs in the blocking CI job** |
| `test_tenant_isolation.py` | No query can cross `clinic_id`. Attempted cross-tenant read returns empty, not another clinic's row |
| `test_injection.py` | Caller speech cannot redefine goals or trigger a tool call. Containment, not detection |
| `test_redaction.py` | Card-number and ID patterns are redacted from transcripts and logs before persistence |
| `test_undo.py` | ✅ **Done** — in `test_booking_rules.py`. All three branches, and undo asserted to move pointers rather than create or drop rows |

## Conventions

- AAA structure: Arrange, Act, Assert.
- Descriptive names that state the behaviour: `test_reschedule_rejected_when_dob_mismatch`.
- Coverage target 80% minimum, but coverage is a floor, not the goal — `test_prohibited.py` passing matters more than the number.
- Fix the implementation, not the test, unless the test is wrong.
