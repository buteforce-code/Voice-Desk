"""Written form in, spoken form out. The last thing before text becomes audio.

`prompts.py` asks the model to say "two fifteen in the afternoon" rather than
"2:15 PM", to read a phone number back in groups, and to never emit markdown.
Those are three validators living in a prompt, and hard rule 9 says a validator
that lives only in a prompt is not a validator. This module is where they go.

**The seam this module sits on, which is the whole reason it is not in
`agent.py`.** There are two renderings of one utterance and they must not be
confused:

    trace.spoken_text   what the agent said, in written form.
                        Goes to the transcript, the audit row, the evals.
    for_speech(...)     the same utterance rendered for a synthesiser.
                        Goes to the TTS request and nowhere else.

Normalising `spoken_text` in place looks tidier and would blind the eval judge
in a way nothing would report. `evals/judge.py` finds ungrounded claims by
matching digits -- `_PHONE` on `[6-9]\\d{9}`, `_clock_claims` on `H:MM`,
`_money_claims` on an amount. Turn "2:15 PM" into "two fifteen in the
afternoon" upstream of that and the scorer stops seeing a time claim at all: it
does not fail, it counts zero claims and reports the call perfectly grounded.
A grounding scorer that has been quietly disarmed is worse than no scorer.

So this runs at the synthesis boundary, in `demo/server._tts`, and every TTS
provider path goes through it.

**What it does NOT do for Tamil and Hindi.** Number-to-words is English-only.
Tamil numerals carry sandhi -- twenty-one is இருபத்தி ஒன்று, not இருபது ஒன்று --
and shipping a table I have not had a Tamil speaker check would produce a line
that is confidently wrong in the caller's own language, which is worse than
digits. For those two the structural work still happens (markup, timestamps,
digit grouping, the period-of-day word) and the digits are left for Sarvam's
`enable_preprocessing`, which is built for exactly this and is now switched on.
That is a known limit, written down rather than papered over.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["for_speech", "sentences"]

DEFAULT_ZONE = "Asia/Kolkata"

# -- number words ----------------------------------------------------------

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)
_ORDINALS = {
    1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth",
    9: "ninth", 12: "twelfth",
}

_DIGIT_WORD = {
    "en-IN": _ONES[:10],
    # Digits read one at a time are the one number form worth doing in all
    # three languages: a phone number is the thing a caller writes down, and
    # a synthesiser handed ten digits in a row reads them as a quantity --
    # "nine billion, eight hundred and seventy-six million...". Ten separate
    # words cannot be misread that way in any language.
    "ta-IN": ("பூஜ்யம்", "ஒன்று", "இரண்டு", "மூன்று", "நான்கு",
              "ஐந்து", "ஆறு", "ஏழு", "எட்டு", "ஒன்பது"),
    "hi-IN": ("शून्य", "एक", "दो", "तीन", "चार",
              "पाँच", "छह", "सात", "आठ", "नौ"),
}

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)

# Period of day, in each language. The boundaries are a clinic's, not a
# dictionary's: an appointment at five is an evening appointment to a patient
# and an afternoon one to a 24-hour clock.
_PERIOD = {
    "en-IN": ("at night", "in the morning", "in the afternoon", "in the evening"),
    "ta-IN": ("இரவு", "காலை", "மதியம்", "மாலை"),
    "hi-IN": ("रात", "सुबह", "दोपहर", "शाम"),
}

_DOCTOR_WORD = {"en-IN": "Doctor", "ta-IN": "டாக்டர்", "hi-IN": "डॉक्टर"}

_AT = {"en-IN": " at ", "ta-IN": " ", "hi-IN": " "}
"""What joins a date to a time.

English needs the preposition. Tamil and Hindi juxtapose -- "29 August காலை
9:30" is how it is said, and an English "at" wedged into the middle of a Tamil
sentence is the kind of seam that makes a line sound machine-assembled.
"""


def _period_index(hour: int) -> int:
    if hour < 5:
        return 0
    if hour < 12:
        return 1
    if hour < 17:
        return 2
    if hour < 21:
        return 3
    return 0


def say_int(n: int) -> str:
    """An integer as a person reads it aloud. English, 0 to 999,999.

    No "and" before the tens -- "five hundred twenty", not "five hundred and
    twenty". Both are said; only one of them survives a synthesiser without
    an audible stumble at the conjunction.
    """
    if n < 0:
        return "minus " + say_int(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        out = f"{_ONES[hundreds]} hundred"
        return f"{out} {say_int(rest)}" if rest else out
    if n < 100_000:
        thousands, rest = divmod(n, 1000)
        out = f"{say_int(thousands)} thousand"
        return f"{out} {say_int(rest)}" if rest else out
    lakhs, rest = divmod(n, 100_000)
    out = f"{say_int(lakhs)} lakh"
    return f"{out} {say_int(rest)}" if rest else out


def say_ordinal(n: int) -> str:
    """1 to 31, as a date is spoken: "the twenty-ninth"."""
    if n in _ORDINALS:
        return _ORDINALS[n]
    if n < 20:
        return _ONES[n] + "th"
    tens, ones = divmod(n, 10)
    if not ones:
        return _TENS[tens][:-1] + "ieth"
    return f"{_TENS[tens]}-{_ORDINALS.get(ones, _ONES[ones] + 'th')}"


def _spell_digits(digits: str, language: str) -> str:
    """A number read one digit at a time, grouped for writing down.

    Indian mobile numbers are said in groups, never as one run of ten, and the
    grouping is what makes a number writeable at the other end. The comma is
    doing prosodic work: every synthesiser here treats it as a short pause,
    which is the gap a caller needs to keep up with a pen.

    Four-three-three for a ten-digit mobile, because that is how the number is
    said here. Threes for anything else long enough to need help. Nothing
    under seven digits is grouped at all -- a five-digit reference broken as
    "four seven two one, one" sounds like two numbers, which is the exact
    failure grouping exists to prevent.
    """
    words = _DIGIT_WORD.get(language, _DIGIT_WORD["en-IN"])
    said = [words[int(d)] for d in digits if d.isdigit()]
    if len(said) < 7:
        return " ".join(said)
    if len(said) == 10:
        groups = [said[0:4], said[4:7], said[7:10]]
    else:
        groups = [said[i : i + 3] for i in range(0, len(said), 3)]
    return ", ".join(" ".join(g) for g in groups if g)


# -- clock and calendar ----------------------------------------------------


def _say_clock(hour: int, minute: int, language: str) -> str:
    """A wall-clock time, in the caller's language.

    Noon and midnight are named rather than numbered because "twelve o'clock
    in the afternoon" is not a thing anyone says, and a caller who hears it
    hears a machine.
    """
    period = _PERIOD.get(language, _PERIOD["en-IN"])[_period_index(hour)]
    hour12 = hour % 12 or 12

    if language != "en-IN":
        # Digits kept, period word added. `enable_preprocessing` voices the
        # digits; what it cannot know is whether nine means morning or night,
        # and on a booking line that is the half that matters.
        clock = f"{hour12}:{minute:02d}" if minute else f"{hour12}"
        return f"{period} {clock}"

    if minute == 0 and hour == 12:
        return "twelve noon"
    if minute == 0 and hour == 0:
        return "midnight"
    if minute == 0:
        return f"{_ONES[hour12]} o'clock {period}"
    if minute < 10:
        # "nine oh five". Said "nine five" it is heard as two numbers.
        return f"{_ONES[hour12]} oh {_ONES[minute]} {period}"
    return f"{_ONES[hour12]} {say_int(minute)} {period}"


def _say_date(when: datetime, language: str, *, with_weekday: bool = True) -> str:
    month = _MONTHS[when.month - 1]
    if language != "en-IN":
        # Month names are said in English by Tamil and Hindi speakers far more
        # often than the calendrical Tamil month, which names a different
        # thing entirely -- ஆவணி is not August.
        return f"{when.day} {month}"
    day = f"the {say_ordinal(when.day)} of {month}"
    return f"{when.strftime('%A')}, {day}" if with_weekday else day


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_ZONE)


# -- the passes ------------------------------------------------------------

_MARKUP = (
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)"), r"\1"),
    (re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-*•‣●]\s+", re.M), ""),
    (re.compile(r"^\s*\d{1,2}[.)]\s+", re.M), ""),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
)
"""Markup a synthesiser reads out loud.

Not defence against a model that ignores the prompt -- although it is that
too. It is defence against a model that obeys it: the model is told to name a
doctor exactly as the clinic writes it, and a clinic that writes
`Dr. Ragunandan (Cardiology)` into config has put a bracket in the agent's
mouth. The bullet and heading rules exist because a model asked for a list of
specialties produces one, and every leading hyphen is a spoken "dash".
"""

_ISO = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?"
    r"(Z|[+-]\d{2}:?\d{2})?"
)
_CLOCK_12 = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\.?\b", re.I)
_CLOCK_24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_CLOCK_WORD = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s*([ap])\.?\s?m\.?\b",
    re.I,
)
"""The hour already spelled out, with a meridiem still attached.

The prompt asks for "nine in the morning" and the model half-complies: it
spells the hour and keeps the A M, which is the part a synthesiser reads as
two letters. "Nine AM" is not caught by the digit patterns above and was the
commonest survivor of the whole pipeline.
"""
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_SLASH = re.compile(r"\b(\d{1,2})[/](\d{1,2})[/](\d{2,4})\b")
_PHONE = re.compile(r"(?<!\d)(\+?91[\s-]?)?([6-9]\d{9})(?!\d)")
"""No `\\b` in front of the subscriber number, and that is not a tidy-up.

`\\b[6-9]` cannot match inside `+919876543210`, because the boundary between
the 1 of the country code and the 9 of the number does not exist -- both are
word characters. The whole pattern failed, the number fell through to the
generic long-digit rule, and the caller heard a bare "plus" followed by twelve
digits with the country code buried in the middle of the grouping.
"""
_DIGIT_RUN = re.compile(r"\b\d{5,}\b")
_MONEY = re.compile(
    r"(?:(?:₹|Rs\.?|INR)\s?(\d[\d,]*)|(\d[\d,]*)\s*(?:rupees|rs\.?))",
    re.I,
)
_BARE = re.compile(r"(?<![\w.:/-])(\d{1,4})(?![\w.:/-])")
_DOCTOR = re.compile(r"\bDr\.?(?=\s)", re.I)

_ABBREV = (
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"\be\.g\.,?", re.I), "for example"),
    (re.compile(r"\bi\.e\.,?", re.I), "that is"),
    (re.compile(r"\betc\.", re.I), "and so on"),
    (re.compile(r"\bappt\.?\b", re.I), "appointment"),
    (re.compile(r"\bOPD\b"), "O P D"),
    (re.compile(r"\bIST\b"), "India time"),
)


def _iso_pass(text: str, language: str, tz: ZoneInfo) -> str:
    """Whole timestamps first, before anything can eat half of one.

    `find_slots` returns absolute UTC stamps and the model repeats them --
    the eval judge has a note about a single timestamp being scored as three
    separate claims, which is the same leak seen from the scoring side. Read
    literally by a voice it is "two thousand and twenty-six dash zero eight
    dash twenty-nine T zero nine colon thirty colon zero zero plus...".
    """

    def render(m: re.Match[str]) -> str:
        year, month, day, hour, minute, offset = m.groups()
        try:
            stamp = datetime(
                int(year), int(month), int(day), int(hour), int(minute),
                tzinfo=_offset_zone(offset, tz),
            ).astimezone(tz)
        except ValueError:
            return m.group(0)
        joiner = _AT.get(language, _AT["en-IN"])
        return (
            _say_date(stamp, language)
            + joiner
            + _say_clock(stamp.hour, stamp.minute, language)
        )

    return _ISO.sub(render, text)


def _offset_zone(offset: str | None, fallback: ZoneInfo) -> tzinfo:
    if offset in (None, ""):
        # A naive stamp is already clinic-local everywhere in this codebase.
        return fallback
    if offset == "Z":
        return UTC
    sign = -1 if offset.startswith("-") else 1
    body = offset[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(body[:2]), minutes=int(body[2:4])))


def _clock_pass(text: str, language: str) -> str:
    """Every clock shape, in one pass that may be run twice safely.

    The re-run is not hypothetical, it is the previous pass. `_iso_pass`
    renders a timestamp into a date plus a time, and for Tamil and Hindi that
    time still contains DIGITS -- "காலை 9:30" -- because number-to-words is
    English-only here. `_CLOCK_24` then matched the 9:30 it had just produced
    and prefixed a second period word.

    It was not merely ugly, it was wrong. The re-match sees the 12-hour value,
    so 17:30 came out of `_iso_pass` as "शाम 5:30" and back out of this pass as
    "शाम सुबह 5:30" -- evening and morning, in one breath, about the same
    appointment. A patient sorting that out picks one.

    So a clock already carrying its period word is left alone.
    """
    spoken_already = re.compile(
        "(?:" + "|".join(re.escape(p) for p in _PERIOD.get(language, ())) + r")\s*$"
    )

    def done(m: re.Match[str]) -> bool:
        return bool(spoken_already.search(m.string[: m.start()]))

    def twelve(m: re.Match[str]) -> str:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if not 1 <= hour <= 12 or minute > 59 or done(m):
            return m.group(0)
        hour24 = (hour % 12) + (12 if meridiem == "p" else 0)
        return _say_clock(hour24, minute, language)

    def twentyfour(m: re.Match[str]) -> str:
        if done(m):
            return m.group(0)
        return _say_clock(int(m.group(1)), int(m.group(2)), language)

    def worded(m: re.Match[str]) -> str:
        if done(m):
            return m.group(0)
        hour = _ONES.index(m.group(1).lower())
        hour24 = (hour % 12) + (12 if m.group(2).lower() == "p" else 0)
        return _say_clock(hour24, 0, language)

    out = _CLOCK_12.sub(twelve, text)
    out = _CLOCK_24.sub(twentyfour, out)
    return _CLOCK_WORD.sub(worded, out)


def _date_pass(text: str, language: str) -> str:
    def iso(m: re.Match[str]) -> str:
        try:
            when = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return m.group(0)
        return _say_date(when, language)

    def slash(m: re.Match[str]) -> str:
        day, month, year = (int(g) for g in m.groups())
        year += 2000 if year < 100 else 0
        # Day-first. India writes 03/04 as the third of April, and an American
        # reading would move an appointment by a month without a word of
        # warning to anyone.
        try:
            when = datetime(year, month, day)
        except ValueError:
            return m.group(0)
        return _say_date(when, language)

    return _DATE_SLASH.sub(slash, _DATE_ISO.sub(iso, text))


def _phone_pass(text: str, language: str) -> str:
    def phone(m: re.Match[str]) -> str:
        prefix = "plus nine one, " if m.group(1) else ""
        return prefix + _spell_digits(m.group(2), language)

    text = _PHONE.sub(phone, text)
    # Anything else five digits or longer -- a booking reference, a truncated
    # number -- is an identifier, not a quantity. Nobody wants a reference
    # read as "forty-seven thousand two hundred and eleven".
    return _DIGIT_RUN.sub(lambda m: _spell_digits(m.group(0), language), text)


def _money_pass(text: str, language: str) -> str:
    def money(m: re.Match[str]) -> str:
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        if not raw.isdigit():
            return m.group(0)
        amount = int(raw)
        if language != "en-IN":
            return f"{amount} ரூபாய்" if language == "ta-IN" else f"{amount} रुपये"
        return f"{say_int(amount)} rupees"

    return _MONEY.sub(money, text)


def _bare_pass(text: str) -> str:
    """Whatever integers survived the specific passes. English only.

    Last, and only after times, dates, phone numbers and amounts have claimed
    theirs -- a pass that ran first would turn the 15 in "2:15 PM" into
    "fifteen" and leave the colon behind.
    """
    return _BARE.sub(lambda m: say_int(int(m.group(1))), text)


_SENTENCE = re.compile(r"(?<=[.!?।])\s+")
_WS = re.compile(r"[ \t]+")


def for_speech(
    text: str, language: str = "en-IN", *, timezone: str | None = None
) -> str:
    """Render one agent utterance the way it should be heard.

    Idempotent on text that is already spoken form, which matters because
    nothing downstream tracks whether it has run. Never raises: a malformed
    date or an unparseable offset returns the span untouched rather than
    failing the turn, because the failure mode of this module must be a
    slightly robotic sentence and never silence.
    """
    if not text or not text.strip():
        return ""

    tz = _zone(timezone)
    out = text
    for pattern, replacement in _MARKUP:
        out = pattern.sub(replacement, out)

    out = _DOCTOR.sub(_DOCTOR_WORD.get(language, "Doctor"), out)
    for pattern, replacement in _ABBREV:
        out = pattern.sub(replacement, out)

    out = _iso_pass(out, language, tz)
    out = _money_pass(out, language)
    out = _phone_pass(out, language)
    out = _clock_pass(out, language)
    out = _date_pass(out, language)
    if language == "en-IN":
        out = _bare_pass(out)

    # Newlines become sentence breaks, not silence. A synthesiser handed a
    # bare newline either ignores it or pauses for a beat that reads as the
    # line having dropped.
    # A line that does not already end in punctuation gets a full stop; one
    # that does just gets the space. Stripping "**Available:**" leaves a colon,
    # and the blunt version turned it into "Available:." -- which every
    # synthesiser reads as a stumble.
    out = re.sub(r"([^\s.!?।:;,])[ \t]*\n+[ \t]*", r"\1. ", out)
    out = re.sub(r"\s*\n+\s*", " ", out)
    return _recapitalise(_WS.sub(" ", out).strip())


_SENTENCE_START = re.compile(r"(^|[.!?।]\s+)([a-z])")


def _recapitalise(text: str) -> str:
    """Put back the capital the substitutions ate.

    "Nine AM works" becomes "nine o'clock in the morning works", because the
    replacement is built from a lowercase table. No synthesiser here changes
    its delivery over a capital letter, so this buys nothing acoustically --
    it is for the transcript, the log line and the person reading either one
    while trying to work out whether the renderer misfired.
    """
    return _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def sentences(text: str, limit: int = 4) -> list[str]:
    """Split an utterance into what can be synthesised independently.

    Two things at once, and the second is the one worth the function.

    **Latency.** Bulbul returns a whole JSON document with base64 inside, so
    there is nothing to stream -- a two-sentence reply is silent until the
    second sentence has been synthesised. Split it and time-to-first-audio
    becomes the cost of sentence one alone, with the rest synthesising in
    parallel behind it.

    **Prosody.** Two sentences synthesised as one blob run together at the
    full stop. Played as two clips there is a real gap where a person would
    breathe -- which is free, and is most of what "sounds human" means on a
    read-back.

    Capped, because past a handful of clips the parallel requests cost more
    than the gap is worth, and a reply that long has already broken the
    two-sentence rule.
    """
    parts = [p.strip() for p in _SENTENCE.split(text.strip()) if p.strip()]
    if len(parts) <= limit:
        return parts
    return [*parts[: limit - 1], " ".join(parts[limit - 1 :])]
