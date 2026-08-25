# State Machine (G3)

States live in the database, not in the model's head. Every transition is written to `call_state_transitions` with a reason. A run is replayable from that table alone.

Spine per `agent_build_standard.md` §3:

```
intake → research → draft → validate → approval → execute → audit
```

## States

| State | Meaning | Exit condition |
|---|---|---|
| `intake` | Call answered. AI disclosure spoken. Consent captured. Language detected | Consent artefact row written |
| `identify` | Caller matched to a patient record, or marked new | Match, new-patient, or 3 failed attempts → `transfer` |
| `research` | Intent classified; slots / existing appointment looked up | Intent is one of `book·reschedule·cancel·faq`, and lookup returned |
| `draft` | The proposed action is fully formed as a struct. **Nothing is written** | A complete `ProposedAction` exists |
| `validate` | G6 deterministic validators run against the draft | All validators pass, or → `repair` |
| `repair` | One bounded retry: re-ask the caller for the failing field | Fixed → `validate`; still failing → `transfer` |
| `approval` | Caller confirms verbally. At stages before *approval-gated execution*, clinic staff confirm too | Explicit confirmation captured in transcript + struct |
| `execute` | The single authorized write happens | Tool returns success + idempotency key recorded |
| `audit` | Trace, transcript, consent, action row persisted. Confirmation SMS queued | All rows committed |
| `wrap` | Closing line, call ends normally | Terminal |

## Lateral and terminal states

| State | Reachable from | Notes |
|---|---|---|
| `transfer` | **any** | The safe default. Always permitted, never blocked. Carries context to the human |
| `abandoned` | any | Caller hung up. Any held slot is released |
| `failed` | any | System error. Held slot released, dead-letter row written, alert fired |
| `refused` | any | Caller requested something prohibited (C13–C15). Refusal + transfer offer |

## Transition rules

1. **Forward only**, except `validate → repair → validate` (max one repair loop) and lateral moves to `transfer` / `abandoned` / `failed` / `refused`.
2. **`execute` is reachable only from `approval`.** No other edge exists. This is the single most important invariant in the system.
   Implemented in `state.py`: asserted over the edge table, again at import time (so a shortcut stops the process from starting), and enforced by `registry._authorize`, which requires `state == execute` **plus** the approval token. Requiring only the token would leave the graph decorative; requiring only the state would let any path in. See PROJECT.md D13.
3. **`draft` never writes.** A draft is a struct in memory plus a row in `call_turns`. If the call drops in `draft`, nothing happened.
4. **Slot holds are not bookings — and a booking requires one.** A short TTL hold prevents a race; it expires on its own, is released on `abandoned` / `failed`, and is cleared once the appointment exists (the appointment is the stronger claim; a stale hold hides a slot a later cancellation freed).
   Since 2026-08-21 the hold is also *load-bearing*: `confirm_booking` refuses any slot this call does not currently hold, so which appointment gets made is a server-side fact with a TTL rather than a UUID the model repeated back. See PROJECT.md D25.
5. **Every transition records** `from_state`, `to_state`, `reason`, `at`, and the prompt/model/tool versions in force.
6. **The caller's yes is read twice per turn — before the model moves, and after its tools have run.** Consent is detected in code from the caller's own words, and a hold is what gives those words a referent. Reading only at the top of the turn made the *order of two events inside one turn* decide whether a call could be booked at all: a caller who picks a slot and agrees to it in one breath speaks their yes before the `hold_slot` that pins it, and the yes was discarded. Nothing about the control is weaker — same words, same code, still refused unless exactly one slot is pinned, negation still dominant. See PROJECT.md D26.

## Diagram

```
  intake ──▶ identify ──▶ research ──▶ draft ──▶ validate ──▶ approval ──▶ execute ──▶ audit ──▶ wrap
     │           │            │          │          │  ▲          │            │
     │           │            │          │          ▼  │          │            │
     │           │            │          │       repair┘          │            │
     │           │            │          │          │             │            │
     └───────────┴────────────┴──────────┴──────────┴─────────────┴────────────┘
                                    │
                  ┌─────────────────┼──────────────────┬─────────────────┐
                  ▼                 ▼                  ▼                 ▼
              transfer         abandoned            failed           refused
```

## Undo

Per G3, **undo exists for every reversible executed action before that action ships.**

| Action | Undo | Window |
|---|---|---|
| `confirm_booking` | Soft-cancel the created row, restore slot | `BOOKING_UNDO_WINDOW_SEC` (default 900s) |
| `reschedule` | Restore the superseded version | same |
| `cancel` | Restore the cancelled version | same |
| `send_confirmation` | Not reversible — send a correction message | n/a |

Appointments are **soft-versioned**: a change writes a new row and sets `superseded_by` on the old one. Nothing is ever overwritten, nothing is ever hard-deleted. Undo is therefore a pointer move, not a reconstruction.

The undo window is **autonomy with a grace period, not approval** — recorded as such in `PROJECT.md` §2.2. It never substitutes for the synchronous confirmation in `approval`.

## Uncertainty handling

The agent transfers rather than guesses. Explicit triggers:

- ASR confidence below threshold on a name, date or doctor
- Two candidate doctors match the caller's description
- Intent unclassified after one clarifying question
- Any validator failure surviving `repair`
- Any utterance classified as clinical (C13)
- Caller asks for a human — honoured immediately, no retention attempt
