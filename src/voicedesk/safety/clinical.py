"""Output-side clinical guard — C13 and C14.

Every other prohibited capability is enforced by absence. There is no outbound
dialer, no payment tool, no DELETE grant, no clinical table. C13 is the one
that cannot work that way: **a model can simply say the words.** Removing a
code path does not stop an utterance.

PROJECT.md 2.1 is explicit about what that means here:

    A prompt saying "never give medical advice" is not a control; C13's
    output-side classifier is.

That classifier did not exist until 2026-08-19. Thirty-one eval cases probed a
control that was not there, and the risk register listed it as the enforcement
mechanism for a prohibited capability.

DESIGN

Deterministic, not a model call. G6 requires every validator to be
independently runnable and testable, and a guard that asks an LLM whether an
LLM just gave medical advice shares its failure modes and its jailbreaks.

**The error costs are asymmetric and the thresholds reflect it.** A false
positive is an unnecessary transfer to the front desk: mildly wasteful, and
transfer is the documented safe default anyway. A false negative is a voice
agent giving a patient medical advice, which is the failure this whole project
is shaped around avoiding. Tuned to over-refuse.

WHY IT IS NOT A KEYWORD LIST

Clinical nouns are not violations. Callers say "cardiology", the agent reads
prep instructions aloud, and a keyword matcher fires on both. What makes an
utterance clinical is the *frame* around the noun: a directive ("you should
stop"), an inference ("that sounds like"), an urgency judgement, a dosage, or
a claim about the caller's records. Frames are matched, and most require a
clinical term inside them to fire at all.

GROUNDED CONTENT IS NOT ADVICE

Tenant config legitimately contains instructions — "do not eat for four hours
before the appointment" is a directive with a clinical shape, retrieved from a
config key with a source. `grounded_spans` carries the values the agent
actually retrieved this turn; they are neutralised before classification.
Those values come from tool output, never from the model, so they are
trustworthy in a way the model's own sentences are not.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

import structlog

log = structlog.get_logger(__name__)


class ClinicalCategory(str, Enum):
    ADVICE = "advice"
    """A directive about medication, treatment or what to do about a body."""

    TRIAGE = "triage"
    """An urgency judgement. Both directions are prohibited -- telling someone
    it is an emergency, and telling them it can wait."""

    INTERPRETATION = "interpretation"
    """Mapping described symptoms onto a cause, a condition or a specialty."""

    DIAGNOSIS = "diagnosis"
    RESULTS = "results"
    """C14. Includes confirming that a report merely exists."""

    PRESCRIPTION = "prescription"


@dataclass(frozen=True)
class Verdict:
    permitted: bool
    categories: tuple[ClinicalCategory, ...] = ()
    evidence: tuple[str, ...] = ()
    """What matched. Goes to the audit row -- a refusal nobody can explain is
    a refusal that gets switched off."""

    @property
    def blocked(self) -> bool:
        return not self.permitted


# --------------------------------------------------------------------------
# Lexicons. Tamil and Hindi are first-class: an agent whose guardrails only
# work in English is an agent with no guardrails for most of its callers.
# malicious-012 exists precisely to catch that asymmetry.
# --------------------------------------------------------------------------

_CLINICAL_TERMS = (
    # English
    r"medicine|medication|tablet|tablets|pill|pills|dose|dosage|drug|drugs"
    r"|antibiotic|painkiller|insulin|steroid|injection|ointment|syrup"
    r"|symptom|symptoms|pain|fever|infection|disease|illness|condition"
    r"|bp|blood pressure|sugar|diabetes|cholesterol|thyroid"
    r"|treatment|therapy|surgery|operation|scan|x-?ray|biopsy"
    # Tamil
    r"|மாத்திரை|மருந்து|ஊசி|அறிகுறி|நோய்|காய்ச்சல்|வலி|சர்க்கரை|ரத்த அழுத்தம்|சிகிச்சை"
    # Hindi
    r"|दवा|दवाई|गोली|खुराक|इंजेक्शन|लक्षण|बीमारी|बुखार|दर्द|शुगर|इलाज|मरहम"
    # English medical vocabulary written in Devanagari and Tamil script.
    #
    # Added after "शायद यह कोई इंफेक्शन हो सकता है" passed the guard: the
    # inference frame matched, found no clinical term nearby, and was filtered
    # out -- because the lexicon held only native-script words and the caller
    # used the transliterated English one.
    #
    # This is not an edge case. D4 records that code-switching within a single
    # utterance is the norm rather than the exception for these callers, and
    # borrowed clinical vocabulary is the most code-switched register there is:
    # almost nobody says "प्रतिजैविक" when they mean antibiotic. A lexicon that
    # only covers the formal native word is a lexicon that misses how people
    # actually speak, which would have made the Hindi and Tamil guardrails much
    # weaker than the English one while every test still passed.
    r"|इंफेक्शन|इन्फेक्शन|टैबलेट|टेबलेट|डोज़|डोज|रिपोर्ट|टेस्ट|स्कैन"
    r"|ऑपरेशन|चेकअप|एंटीबायोटिक|प्रेशर|थायराइड|कोलेस्ट्रॉल"
    r"|இன்ஃபெக்ஷன்|டேப்லெட்|டோஸ்|ரிப்போர்ட்|டெஸ்ட்|ஸ்கேன்"
    r"|ஆபரேஷன்|செக்கப்|ஆண்டிபயாட்டிக்|பிரஷர்|தைராய்டு"
)

_DIRECTIVE = (
    r"you (?:should|must|need to|ought to|have to|can|may)"
    r"|(?:i|we) (?:recommend|suggest|advise)"
    r"|(?:please )?(?:take|stop|start|continue|increase|reduce|skip|double)"
    r"|try (?:taking|using)"
    r"|எடுத்துக்|சாப்பிடுங்க|நிறுத்துங்க|கூட்டுங்க|குறைக்க"
    r"|लीजिए|लीजिये|ले लीजिए|लें|बंद कर|शुरू कर|बढ़ा|कम कर"
)

_INFERENCE = (
    r"(?:it|that|this) (?:sounds|seems|looks) like"
    r"|(?:could|might|may|must) be"
    r"|probably|likely|it'?s probably"
    r"|you (?:probably )?have|you'?ve got|you are having"
    r"|(?:in )?that case (?:it|you)"
    r"|மாதிரி இருக்கு|இருக்கலாம்|தான் போல"
    r"|लगता है|हो सकता है|शायद|जैसा लग"
)

_URGENCY = (
    r"emergency|urgent(?:ly)?|immediately|right away|straight away"
    r"|casualty|a&e|icu|ambulance|108"
    r"|(?:can|could) wait|no(?:t)? (?:urgent|serious)|nothing serious"
    r"|don'?t worry|no need to (?:worry|rush|come in)"
    r"|அவசர|உடனே|பயப்பட வேண்டாம்|காத்திருக்கலாம்"
    r"|आपातकाल|तुरंत|घबराइए मत|चिंता की बात नहीं|रुक सकते"
)

_RESULT_CLAIM = (
    r"your (?:report|reports|result|results|scan|test|tests|blood work|levels?)"
    r"|(?:report|result|results|scan|test|blood work) "
    r"(?:is|are|was|were|came|shows?|showed|indicates?)"
    # "came back negative" is how a result is actually delivered out loud, and
    # it names no report and uses no copula -- so the two patterns above both
    # miss it. Found by test, not by review.
    r"|came back (?:normal|abnormal|negative|positive|clear|fine|okay|ok)"
    r"|(?:is|are) (?:normal|abnormal|negative|positive|elevated|low|high)"
    r"|உங்க (?:ரிப்போர்ட்|முடிவு)|ரிப்போர்ட் வந்து"
    r"|आपकी (?:रिपोर्ट|जांच)|रिपोर्ट (?:आ गई|में)"
)

# A number next to a unit of medicine. Fires on its own -- there is no benign
# reason for a scheduling agent to say "500 mg" or "two tablets twice a day".
_DOSAGE = re.compile(
    r"\b\d+\s*(?:mg|mcg|ml|g|iu|units?)\b"
    r"|\b(?:one|two|three|four|half|1|2|3|4)\s+"
    r"(?:tablet|tablets|pill|pills|spoon|spoons|drops?|மாத்திரை|गोली|गोलियां)\b"
    r"|\b(?:once|twice|thrice|\d+\s*times)\s+(?:a|per)\s+day\b"
    r"|\b(?:bd|tds|od|qid|sos)\b",
    re.IGNORECASE,
)

_TERMS = re.compile(_CLINICAL_TERMS, re.IGNORECASE)
_FRAMES: tuple[tuple[ClinicalCategory, re.Pattern[str], bool], ...] = (
    # (category, frame, requires a clinical term nearby)
    (ClinicalCategory.ADVICE, re.compile(_DIRECTIVE, re.IGNORECASE), True),
    (ClinicalCategory.INTERPRETATION, re.compile(_INFERENCE, re.IGNORECASE), True),
    (ClinicalCategory.TRIAGE, re.compile(_URGENCY, re.IGNORECASE), False),
    (ClinicalCategory.RESULTS, re.compile(_RESULT_CLAIM, re.IGNORECASE), False),
)

REFUSALS = {
    "en-IN": (
        "I'm not able to help with anything medical — I only handle "
        "appointments. Let me put you through to the front desk."
    ),
    "ta-IN": (
        "மருத்துவம் சம்பந்தமான விஷயங்களுக்கு என்னால் உதவ முடியாது. நான் "
        "அப்பாயின்ட்மென்ட் மட்டும் தான் பார்க்கிறேன். உங்களை front desk-க்கு "
        "இணைக்கிறேன்."
    ),
    "hi-IN": (
        "मैं चिकित्सा से जुड़ी किसी बात में मदद नहीं कर सकता — मैं सिर्फ़ "
        "अपॉइंटमेंट देखता हूँ। मैं आपको फ़्रंट डेस्क से जोड़ता हूँ।"
    ),
}

_NEAR = 60
"""Characters between a frame and a clinical term for them to count as one
claim. Wide enough for "you should probably stop taking that tablet", narrow
enough that a directive in one sentence and a noun two sentences later do not
combine into a violation that was never uttered."""


def _normalise(text: str) -> str:
    """NFKC, collapsed whitespace, lowered.

    Without normalisation, a decomposed Devanagari or Tamil string does not
    match a composed pattern -- and ASR output is not guaranteed to be
    composed. A guard that a normalisation form can bypass is not a guard.
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().lower()


def _strip_grounded(text: str, grounded_spans: tuple[str, ...]) -> str:
    """Remove verbatim config content before classifying.

    Prep instructions are directives with a clinical shape, retrieved from a
    config key with a source. They are not the model's claims, so they are not
    what this guard is looking at.
    """
    for span in grounded_spans:
        cleaned = _normalise(span)
        if len(cleaned) >= 12:
            text = text.replace(cleaned, " ")
    return text


def screen(
    utterance: str,
    *,
    language: str = "en-IN",
    grounded_spans: tuple[str, ...] = (),
) -> Verdict:
    """Classify one agent utterance before it reaches TTS.

    Returns a `Verdict`. A blocked verdict must never be spoken -- use
    `safe_reply` for what to say instead.
    """
    text = _strip_grounded(_normalise(utterance), grounded_spans)
    if not text:
        return Verdict(permitted=True)

    categories: list[ClinicalCategory] = []
    evidence: list[str] = []

    # Dosage stands alone. A scheduling agent has no benign reason to say it.
    dosage = _DOSAGE.search(text)
    if dosage:
        categories.append(ClinicalCategory.PRESCRIPTION)
        evidence.append(f"dosage:{dosage.group().strip()}")

    term_spans = [m.span() for m in _TERMS.finditer(text)]

    for category, frame, needs_term in _FRAMES:
        for match in frame.finditer(text):
            if needs_term and not _near_any(match.span(), term_spans):
                continue
            categories.append(category)
            evidence.append(f"{category.value}:{match.group().strip()}")
            break

    if not categories:
        return Verdict(permitted=True)

    log.warning(
        "clinical.blocked",
        categories=[c.value for c in categories],
        evidence=evidence,
    )
    return Verdict(
        permitted=False,
        categories=tuple(dict.fromkeys(categories)),
        evidence=tuple(evidence),
    )


def _near_any(span: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(
        not (other_end < start - _NEAR or other_start > end + _NEAR)
        for other_start, other_end in others
    )


def safe_reply(language: str = "en-IN") -> str:
    """What to say instead. Refusal plus an immediate transfer offer, per C13.

    Falls back to English rather than raising: a missing translation must not
    become a code path where the original clinical utterance goes out.
    """
    return REFUSALS.get(language, REFUSALS["en-IN"])


def guard_agent_turn(
    utterance: str,
    *,
    language: str = "en-IN",
    grounded_spans: tuple[str, ...] = (),
) -> tuple[str, Verdict]:
    """The wrapper the pipeline calls. Returns what to actually say.

    Deliberately impossible to use wrongly: it hands back the utterance to
    speak, so there is no shape of this call that screens the text and then
    speaks the original anyway.
    """
    verdict = screen(utterance, language=language, grounded_spans=grounded_spans)
    if verdict.blocked:
        return safe_reply(language), verdict
    return utterance, verdict
