"""What the desk says while it is working, and how long the line may go quiet.

**The gap this exists to fill.** `/api/turn` is one blocking request that
returns when the whole turn is finished. A turn that looks a doctor up and then
searches slots is two tool calls and three model round-trips before a single
byte of audio is synthesised, and the caller hears every millisecond of it as
nothing at all. Two seconds of silence on a phone call is not experienced as
latency; it is experienced as the line having dropped, and the caller says
"hello?" into it -- which arrives as a new utterance and costs another turn.

A receptionist does not go silent while they look at a screen. They say "let me
just check that for you" and the typing is audible. That sentence is not
politeness: it is what tells the caller the line is alive and that they should
not speak yet. It buys the whole lookup.

**Why the lines live here and not in the page.** They are caller-facing copy in
three languages, which is what `prompts.py` is for and what the browser is not.
The page fetches them from `/api/config`, so there is one place a Tamil speaker
can correct the Tamil.

**Why they are not model-generated.** A filler must be spoken BEFORE the model
has produced anything -- that is the entire point -- so it cannot come from the
model. It is also the one utterance on the call that must never be interesting:
it carries no information, it must not ask a question, and it must not be
mistaken for the answer. A fixed list of four is the right amount of variety;
a generated one is a second thing that can say something clinical.
"""

from __future__ import annotations

import random

__all__ = ["HOLD_LINES", "FILL_AFTER_MS", "hold_line", "hold_lines_for"]

FILL_AFTER_MS = 700
"""How long the line may be silent before the desk says something.

Set from the shape of the two costs rather than from a preference. Fire too
early and the filler collides with a fast turn -- the reply is ready, and the
caller now waits for "one moment" to finish before hearing it, so a 400ms turn
has been made into a 1.5 second one. Fire too late and the silence has already
been read as a dropped call.

700ms sits above a plain no-tool turn on the current model, which returns in
roughly half a second, and below the point at which a human starts to wonder.
Conversation-analysis work on turn-taking puts the tolerated inter-speaker gap
at a couple of hundred milliseconds in face-to-face talk and longer on the
phone, where the channel itself is expected to be lossy -- so the threshold is
deliberately nearer a second than a quarter of one.

If the model gets faster this should go UP, not down: the filler is only worth
saying when there is something to fill.
"""

HOLD_LINES: dict[str, tuple[str, ...]] = {
    "en-IN": (
        "One moment.",
        "Let me check that.",
        "Just a second, I'm looking.",
        "Right, let me see.",
    ),
    "ta-IN": (
        "ஒரு நிமிஷம்.",
        "செக் பண்றேன்.",
        "ஒரு செகண்ட், பாக்கறேன்.",
        "சரி, பாக்கலாம்.",
    ),
    "hi-IN": (
        "एक मिनट.",
        "मैं चेक करती हूँ.",
        "एक सेकंड, देख रही हूँ.",
        "ठीक है, देखती हूँ.",
    ),
}
"""Four per language, and every one of them under a second to say.

Short because the reply may land at any moment and the caller waits for
whichever is still playing. Spoken register for the same reason `prompts.py`
insists on it -- "செக் பண்றேன்" is what a Chennai receptionist says, and the
literary Tamil for "I shall verify" is how an automated line gives itself away
in the one utterance whose whole job is to sound unremarkable.

None of them is a question. A filler that asks something invites an answer,
and the answer arrives while the real reply is being spoken.
"""


def hold_line(language: str = "en-IN", *, rng: random.Random | None = None) -> str:
    """One filler, varied between turns.

    Varied on purpose. The same three words at every pause is the single most
    recognisable tell of an automated line -- more than the voice, because a
    caller hears it four times in ninety seconds. Four lines in rotation is
    enough that a caller does not notice the repetition inside one call.
    """
    lines = hold_lines_for(language)
    return (rng or random).choice(lines)


def hold_lines_for(language: str) -> tuple[str, ...]:
    """The whole set for one language, for a client that rotates them itself.

    Falls back to English rather than raising, for the same reason the
    disclosure does: a missing translation must never become the path where
    nothing is said at all, and here that path is silence on the line.
    """
    return HOLD_LINES.get(language) or HOLD_LINES["en-IN"]
