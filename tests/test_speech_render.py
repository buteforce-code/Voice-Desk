"""The spoken-form renderer.

Every case here is a sentence this agent actually produces. The times come from
`find_slots`, which returns absolute UTC stamps; the fees come from tenant
config; the phone numbers come from the confirmation read-back the prompt asks
for. None of them are invented shapes.
"""

from __future__ import annotations

import re

import pytest

from voicedesk.voice.pacing import FILL_AFTER_MS, HOLD_LINES, hold_line, hold_lines_for
from voicedesk.voice.speech import for_speech, say_int, say_ordinal, sentences

KOLKATA = "Asia/Kolkata"


def spoken(text: str, language: str = "en-IN") -> str:
    return for_speech(text, language, timezone=KOLKATA)


# -- integers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "zero"),
        (9, "nine"),
        (15, "fifteen"),
        (20, "twenty"),
        (29, "twenty-nine"),
        (100, "one hundred"),
        (500, "five hundred"),
        (1200, "one thousand two hundred"),
    ],
)
def test_say_int(n: int, expected: str) -> None:
    assert say_int(n) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [(1, "first"), (2, "second"), (3, "third"), (11, "eleventh"),
     (20, "twentieth"), (21, "twenty-first"), (29, "twenty-ninth"), (31, "thirty-first")],
)
def test_say_ordinal(n: int, expected: str) -> None:
    assert say_ordinal(n) == expected


# -- clock -----------------------------------------------------------------


def test_afternoon_clock_is_not_read_as_punctuation() -> None:
    assert spoken("I can do 2:15 PM.") == "I can do two fifteen in the afternoon."


def test_on_the_hour_says_o_clock() -> None:
    assert spoken("Nine AM works.") == "Nine o'clock in the morning works."


def test_single_digit_minutes_get_an_oh() -> None:
    # "nine five" is heard as two numbers. Every clinic slot on a five-minute
    # grid hits this.
    assert "nine oh five in the morning" in spoken("How about 9:05 am?")


def test_noon_and_midnight_are_named_not_numbered() -> None:
    # "twelve o'clock in the afternoon" is not a thing anyone says.
    assert "twelve noon" in spoken("The slot is at 12:00 PM.")
    assert "midnight" in spoken("The line closes at 00:00.")


def test_twentyfour_hour_clock_is_converted() -> None:
    assert "five thirty in the evening" in spoken("The slot is at 17:30.")


def test_evening_and_morning_are_distinguished() -> None:
    assert "in the morning" in spoken("9:00 AM")
    assert "in the evening" in spoken("7:00 PM")


# -- timestamps ------------------------------------------------------------


def test_a_whole_iso_stamp_becomes_one_spoken_phrase() -> None:
    """The shape `find_slots` returns, repeated verbatim by the model.

    Read literally this is "two thousand and twenty-six dash zero eight dash
    twenty-nine T zero nine colon thirty colon zero zero plus zero five colon
    thirty" -- eleven seconds of a caller wondering what they have rung.
    """
    said = spoken("Your slot is 2026-08-29T09:30:00+05:30.")
    assert "Saturday, the twenty-ninth of August at nine thirty in the morning" in said
    assert "T0" not in said
    assert "+05:30" not in said


def test_a_utc_stamp_is_converted_into_clinic_time() -> None:
    # 04:00Z is 09:30 in Kolkata. Saying "four in the morning" would send a
    # patient to a closed clinic.
    assert "nine thirty in the morning" in spoken("2026-08-29T04:00:00Z")


def test_a_naive_stamp_is_treated_as_clinic_local() -> None:
    assert "nine thirty in the morning" in spoken("2026-08-29 09:30")


def test_a_malformed_stamp_is_left_alone_rather_than_raising() -> None:
    # Silence is the one failure a voice line may not produce, so an
    # unparseable span comes back untouched.
    assert for_speech("2026-13-45T99:99:00Z", "en-IN", timezone=KOLKATA)


# -- dates -----------------------------------------------------------------


def test_iso_date_becomes_a_spoken_date() -> None:
    assert "Saturday, the twenty-ninth of August" in spoken("Booked for 2026-08-29.")


def test_slash_dates_are_read_day_first() -> None:
    """India writes 03/04 as the third of April.

    A month-first reading moves the appointment by a month and nothing in the
    call would contradict it.
    """
    assert "the third of April" in spoken("03/04/2026")


# -- phone numbers ---------------------------------------------------------


def test_a_mobile_number_is_grouped_not_run_together() -> None:
    said = spoken("I have 9876543210 for you.")
    assert "nine eight seven six, five four three, two one zero" in said
    assert "9876543210" not in said


def test_country_code_is_spoken() -> None:
    # `\b[6-9]` cannot match inside "+91…" -- the boundary between the 1 and
    # the 9 does not exist -- so this used to fall through to the generic
    # long-digit rule with the country code buried in the grouping.
    assert "plus nine one, nine eight seven six" in spoken("We'll text +919876543210.")


def test_a_long_reference_is_spelled_out_not_totalled() -> None:
    # An identifier is not a quantity.
    assert "four seven two one one" in spoken("Reference 47211.")


def test_phone_digits_are_spelled_in_the_callers_language() -> None:
    said = spoken("9876543210", "ta-IN")
    assert "ஒன்பது எட்டு ஏழு ஆறு" in said
    said_hi = spoken("9876543210", "hi-IN")
    assert "नौ आठ सात छह" in said_hi


# -- money -----------------------------------------------------------------


@pytest.mark.parametrize("written", ["Rs. 500", "₹500", "INR 500", "500 rupees"])
def test_fees_are_spoken_as_words(written: str) -> None:
    assert "five hundred rupees" in spoken(f"The consultation is {written}.")


def test_a_thousand_separator_does_not_split_the_amount() -> None:
    assert "one thousand two hundred rupees" in spoken("The scan is ₹1,200.")


# -- markup ----------------------------------------------------------------


def test_markdown_is_stripped_rather_than_read_aloud() -> None:
    said = spoken("**Dr. Ragunandan** is free at 9 AM.")
    assert "*" not in said
    assert said.startswith("Doctor Ragunandan")


def test_bullets_and_numbering_do_not_become_spoken_dashes() -> None:
    said = spoken("- Cardiology\n- Dermatology")
    assert "-" not in said
    assert "Cardiology" in said and "Dermatology" in said


def test_newlines_become_sentence_breaks_not_pauses() -> None:
    assert "\n" not in spoken("Booked.\nAnything else?")


def test_dr_is_expanded_per_language() -> None:
    assert "Doctor Ragunandan" in spoken("Dr. Ragunandan")
    assert "டாக்டர் Ragunandan" in spoken("Dr. Ragunandan", "ta-IN")
    assert "डॉक्टर Ragunandan" in spoken("Dr. Ragunandan", "hi-IN")


# -- indic --------------------------------------------------------------


def test_indic_times_keep_digits_but_gain_the_period_word() -> None:
    """Sarvam voices the digits; only we know morning from night.

    Number-to-words is deliberately not done for Tamil and Hindi -- see the
    module docstring -- but the half that decides whether a patient arrives at
    nine in the morning or nine at night is done here.
    """
    said = spoken("2:15 PM", "ta-IN")
    assert "மதியம்" in said and "2:15" in said

    said_hi = spoken("9:00 AM", "hi-IN")
    assert "सुबह" in said_hi


def test_indic_text_is_not_mangled_by_the_english_number_pass() -> None:
    original = "நாளைக்கு காலை appointment இருக்கு."
    assert spoken(original, "ta-IN") == original


def test_an_indic_timestamp_gets_exactly_one_period_word() -> None:
    """The clock pass must not re-match what the timestamp pass just wrote.

    Indic times keep their digits, so `_CLOCK_24` matched the "9:30" it had
    been handed and prefixed a second period word: "காலை காலை 9:30".
    """
    said = spoken("2026-08-29T09:30:00+05:30", "ta-IN")
    assert said.count("காலை") == 1, said


def test_an_indic_evening_does_not_become_a_morning() -> None:
    """The same bug, in the version that would send a patient to a shut clinic.

    The duplicate match saw the 12-HOUR value, so half past five in the evening
    came back as "शाम सुबह 5:30" -- evening and morning about one appointment.
    """
    said = spoken("2026-08-29T17:30:00+05:30", "hi-IN")
    assert "शाम" in said
    assert "सुबह" not in said, said


def test_indic_dates_and_times_are_not_joined_by_an_english_preposition() -> None:
    assert " at " not in spoken("2026-08-29T09:30:00+05:30", "ta-IN")
    assert " at " in spoken("2026-08-29T09:30:00+05:30", "en-IN")


def test_a_stripped_heading_does_not_leave_a_stumble() -> None:
    # "**Available:**" plus a newline became "Available:." -- read as a stumble.
    assert ":." not in spoken("**Available:**\n- Cardiology at 9 AM")


# -- general ---------------------------------------------------------------


def test_running_twice_changes_nothing() -> None:
    """Idempotent, because nothing downstream tracks whether it has run."""
    once = spoken("Dr. Rao at 2:15 PM on 2026-08-29, ₹500.")
    assert spoken(once) == once


def test_empty_input_is_empty_output() -> None:
    assert for_speech("") == ""
    assert for_speech("   ") == ""


def test_no_digits_survive_an_english_confirmation() -> None:
    """The read-back is the utterance a caller judges the line on."""
    said = spoken(
        "Confirmed: Dr. Ragunandan, 2026-08-29T09:30:00+05:30, "
        "Rs. 500, and we'll text 9876543210."
    )
    assert not re.search(r"\d", said), said


# -- sentence splitting ----------------------------------------------------


def test_sentences_split_on_terminators() -> None:
    assert sentences("Booked. Anything else?") == ["Booked.", "Anything else?"]


def test_the_devanagari_full_stop_is_a_terminator() -> None:
    assert len(sentences("बुक हो गया। और कुछ?")) == 2


def test_splitting_is_capped_and_loses_nothing() -> None:
    text = " ".join(f"Sentence {n}." for n in range(1, 9))
    parts = sentences(text, limit=3)
    assert len(parts) == 3
    assert "Sentence 8." in parts[-1]


def test_a_single_sentence_stays_one_clip() -> None:
    assert sentences("One moment.") == ["One moment."]


# -- pacing ----------------------------------------------------------------


def test_hold_lines_exist_for_every_supported_language() -> None:
    for language in ("en-IN", "ta-IN", "hi-IN"):
        assert len(hold_lines_for(language)) >= 3


def test_an_unknown_language_falls_back_rather_than_going_silent() -> None:
    assert hold_lines_for("fr-FR") == HOLD_LINES["en-IN"]


def test_no_hold_line_asks_a_question() -> None:
    """A filler that invites an answer gets one, on top of the real reply."""
    for lines in HOLD_LINES.values():
        for line in lines:
            assert "?" not in line, line


def test_hold_lines_are_short_enough_to_be_overtaken() -> None:
    for lines in HOLD_LINES.values():
        for line in lines:
            assert len(line) <= 40, line


def test_hold_line_varies_between_turns() -> None:
    seen = {hold_line("en-IN") for _ in range(60)}
    assert len(seen) > 1


def test_fill_threshold_is_under_a_second() -> None:
    assert 300 <= FILL_AFTER_MS <= 1000
