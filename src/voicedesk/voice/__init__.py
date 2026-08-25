"""How the desk sounds, as opposed to what it says.

`agent.py` decides the words. This package decides how they arrive: rendered
for a synthesiser rather than for a reader (`speech`), and paced so the line is
never silent long enough to be mistaken for a dropped call (`pacing`).

Nothing here is a control. The clinical guard, the state machine and the tool
registry have all already run by the time anything in this package sees a
sentence -- these modules may not change what is said, only how it is spoken.
"""

from voicedesk.voice.pacing import FILL_AFTER_MS, HOLD_LINES, hold_line, hold_lines_for
from voicedesk.voice.speech import for_speech, sentences

__all__ = [
    "FILL_AFTER_MS",
    "HOLD_LINES",
    "for_speech",
    "hold_line",
    "hold_lines_for",
    "sentences",
]
