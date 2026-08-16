# Dashboard UX (G3)

> *"Never make a user guess what the agent changed."* — `agent_build_standard.md` §3

Two audiences, one app:

- **Clinic staff** — the live queue. Approve, reject, edit, undo. This is the safety surface at rollout stages `draft-only` and `approval-gated`.
- **Prospects** — the public replay of a completed call. This is the portfolio asset.

## G3 required elements

| Requirement | Where it lives |
|---|---|
| What the agent is doing now | **Live rail** — current state chip, animated, from `calls.state` |
| Sources and evidence used | **Evidence panel** — every factual claim links to the config key or DB row it came from |
| Draft vs final action | **Action diff** — side-by-side `ProposedAction` → written row, changed fields highlighted |
| Confidence and uncertainty | **Turn strip** — per-turn ASR confidence; low-confidence tokens underlined in amber |
| Approve · reject · edit · retry · **undo** | **Action bar**, pinned. Undo persists for the whole window and stays visible after execution |
| Full action history | **Timeline** — every row of `call_state_transitions` and `agent_actions`, in order |

## Screens

### 1. Live queue
Active calls, one row each: clinic · caller (masked) · language · current state · elapsed · latency badge. Rows stream in. Clicking opens the call detail.

Staff-facing controls at gated stages: **Approve** / **Reject** / **Edit slot** on any call sitting in `approval`.

### 2. Call detail
The core screen. Four regions:

```
┌─────────────────────────────────────────────────────────┐
│ state rail   intake ▸ identify ▸ research ▸ draft ▸ ...  │
├──────────────────────────┬──────────────────────────────┤
│ TRANSCRIPT               │ EVIDENCE                     │
│ turn-by-turn, bilingual  │ every claim → its source     │
│ confidence underlines    │ config key or table row      │
│ latency per turn         │                              │
├──────────────────────────┴──────────────────────────────┤
│ ACTION DIFF   proposed ──▶ written    changed fields lit │
├─────────────────────────────────────────────────────────┤
│ [ Approve ] [ Reject ] [ Edit ] [ Retry ]      [ Undo ]  │
└─────────────────────────────────────────────────────────┘
```

Transcript shows **redacted** text only — it reads from `call_turns.text_redacted`. There is no unredacted view in the product.

### 3. Replay (public, the portfolio surface)
A completed call, replayable: audio scrubber synced to the transcript, the state rail advancing, the evidence panel updating, and the appointment row appearing at `execute`. Latency and cost shown as live counters.

This is what a prospect sees instead of a deck. It must be honest — real trace data, no staged numbers.

### 4. Metrics
The §1.5 targets as live tiles: resolution rate · booking accuracy · median and P95 latency · language accuracy · transfer rate · cost per booking. Each tile links to the runs behind it.

## Design language

Per `brand_bible.md`:

- Both themes, user-toggleable. Light default (`#ffffff` / `#f9fafb`).
- Accent **yellow `#FFFC01`**, sparingly — active state chip, undo affordance, nothing else.
- **Monospace for all data** — latency, cost, confidence, IDs, timestamps. Mono reads as credible.
- Display type bold with tight tracking. Framer Motion on all transitions; nothing static.
- Glassmorphism reserved for the metric tiles.
- No stock imagery. No rainbow gradients. No "Book a free call!" CTA anywhere.

## Non-negotiables

1. **Undo is never hidden behind a menu.** It is a top-level control, visible for the full window.
2. **A claim without a source is a bug.** If the evidence panel cannot resolve a factual statement to a config key or a row, the turn is flagged in red and counts as a grounding failure in G5.
3. **Uncertainty is shown, not smoothed.** Low ASR confidence is visible to staff at the moment it matters, not discovered later in a log.
4. **The diff is always rendered**, even when nothing changed — an empty diff is information.
5. **No PII in the public replay.** Caller number masked, name replaced with a token, DOB never rendered.
