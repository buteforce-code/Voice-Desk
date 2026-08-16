# Tests

**G6 — deterministic validators. The LLM reasons. The system validates.**

No validator lives only in a prompt. Each is independently runnable and tested.

## Planned modules

| File | Asserts |
|---|---|
| `test_prohibited.py` | The prohibited row of the risk register (C12–C17) is unreachable: no outbound code path, no DELETE grant, no clinical table grant, no payment tool. **Runs in CI as a blocking job.** |
| `test_slot_validity.py` | Slot exists · is in the future · within OPD hours · doctor works that day · not on a holiday |
| `test_double_booking.py` | Two concurrent confirmations for one slot cannot both win. Idempotency key honoured |
| `test_identity.py` | Reschedule and cancel require verified identity; 3-attempt cap then transfer |
| `test_consent.py` | Consent artefact exists and is keyed to the call trace ID before any write |
| `test_clinical_guard.py` | Output-side classifier catches advice, triage, symptom interpretation, results, prescriptions — tested against paraphrases, not keywords |
| `test_tenant_isolation.py` | No query can cross `clinic_id`. Attempted cross-tenant read returns empty, not another clinic's row |
| `test_injection.py` | Caller speech cannot redefine goals or trigger a tool call. Containment, not detection |
| `test_redaction.py` | Card-number and ID patterns are redacted from transcripts and logs before persistence |
| `test_undo.py` | Every reversible executed action has a working undo **before that action ships** |

## Conventions

- AAA structure: Arrange, Act, Assert.
- Descriptive names that state the behaviour: `test_reschedule_rejected_when_dob_mismatch`.
- Coverage target 80% minimum, but coverage is a floor, not the goal — `test_prohibited.py` passing matters more than the number.
- Fix the implementation, not the test, unless the test is wrong.
