/* Recorded calls.
 *
 * **Every line below is real output**, captured from the running agent on
 * 2026-08-25 against the Meridian demo tenant -- not copy written for a
 * landing page. The tool names and states are what the state machine
 * actually did on those turns.
 *
 * They exist for two jobs:
 *
 *   1. the language showcase, where a visitor should be able to see the same
 *      booking happen in three languages without making three phone calls;
 *   2. the fallback, when the Railway container is cold or a visitor has no
 *      microphone. A hero that shows an error is worse than a hero that
 *      shows a recording, and a recording that is honest about being one
 *      costs nothing.
 *
 * Curated, not invented: these are the good takes. Where a turn showed a bug
 * it was left out of the site and written up instead. */

export type Line = {
  who: "agent" | "caller";
  text: string;
  /** Tool calls the agent made on this turn, as the registry recorded them. */
  tools?: string[];
  /** State machine position after the turn. */
  state?: string;
  /** True for the "one moment" pacing line -- heard, but not part of the
   *  conversation record. Rendered faded, the way the demo does. */
  hold?: boolean;
};

export type Replay = {
  code: string;
  label: string;
  native: string;
  indic: boolean;
  lines: Line[];
};

export const REPLAYS: Replay[] = [
  {
    code: "en-IN",
    label: "English",
    native: "English",
    indic: false,
    lines: [
      {
        who: "agent",
        text: "This is an automated assistant for Meridian Speciality Clinic. How can I help you today?",
        state: "identify",
      },
      { who: "caller", text: "I need a cardiology appointment tomorrow morning" },
      { who: "agent", text: "One moment.", hold: true },
      {
        who: "agent",
        text: "Okay, tomorrow, Wednesday the twenty-sixth of August, Dr. Anitha Varadhan has an opening for Cardiology at ten in the morning. Shall I book that for you?",
        tools: ["find_slots"],
        state: "draft",
      },
      { who: "caller", text: "yes please book it, it's for me, Dhyan, 34, male, use this number" },
      {
        who: "agent",
        text: "Booked. Dr. Anitha Varadhan, Cardiology, tomorrow at ten in the morning. You'll get a confirmation on this number.",
        tools: ["hold_slot", "confirm_booking"],
        state: "wrap",
      },
    ],
  },
  {
    code: "ta-IN",
    label: "Tamil",
    native: "தமிழ்",
    indic: true,
    lines: [
      {
        who: "agent",
        text: "இது Meridian Speciality Clinic-ன் தானியங்கி உதவியாளர். நான் எப்படி உதவ முடியும்?",
        state: "identify",
      },
      { who: "caller", text: "வணக்கம், நாளைக்கு morning ஒரு appointment வேணும்" },
      { who: "agent", text: "ஒரு நிமிஷம்.", hold: true },
      {
        who: "agent",
        text: "நாளைக்கு காலை ஒன்பது மணிக்கு டாக்டர் ரகுநந்தன் ஜெனரல் மெடிசினுக்கு இருக்காரு. இந்த அப்பாயிண்ட்மென்ட் உங்களுக்கு ஓகேவா?",
        tools: ["find_slots"],
        state: "draft",
      },
      {
        who: "caller",
        text: "Cardiology. சரி, அது ok. என் பேரு Dhyan, 34 வயசு, male. இந்த number-ஐயே use பண்ணுங்க",
      },
      {
        who: "agent",
        text: "டாக்டர் அனிதா வரதன் கார்டியாலஜிக்கு நாளைக்கு காலை ஒன்பது மணிக்கு ஒரு அப்பாயிண்ட்மென்ட் இருக்கு. இது உங்களுக்கு ஓகேவா?",
        tools: ["find_slots"],
        state: "draft",
      },
    ],
  },
  {
    code: "hi-IN",
    label: "Hindi",
    native: "हिन्दी",
    indic: true,
    lines: [
      {
        who: "agent",
        text: "यह Meridian Speciality Clinic का स्वचालित सहायक है। मैं आपकी क्या मदद कर सकता हूँ?",
        state: "identify",
      },
      { who: "caller", text: "नमस्ते, मुझे कल सुबह डॉक्टर से मिलना है" },
      { who: "agent", text: "एक सेकंड, देख रही हूँ.", hold: true },
      {
        who: "caller",
        text: "Cardiology ठीक रहेगा. मेरा नाम Dhyan है, 34 साल, male. यही नंबर use कर लीजिए",
      },
      {
        who: "agent",
        text: "कल सुबह Cardiology में डॉक्टर अनिता वरदान नौ बजे उपलब्ध हैं। क्या आप यह अपॉइंटमेंट बुक करना चाहेंगे?",
        tools: ["find_slots"],
        state: "draft",
      },
    ],
  },
];

/* The refusal, shown on its own because it is the product.
 *
 * Real capture. The caller mentions a symptom and asks to be seen; the agent
 * acknowledges it once, does not interpret it, and moves to the thing it can
 * actually do. Choosing a specialty FROM a symptom would be triage and is
 * refused in code -- naming one yourself is fine. */
export const CLINICAL_TURN: Line[] = [
  { who: "caller", text: "my head hurts, can I see a doctor today?" },
  {
    who: "agent",
    text: "I'm sorry to hear your head hurts. I can help you book an appointment to see a doctor today. Which specialty would you like to consult? If you are not sure, most people start with General Medicine.",
    state: "research",
  },
];

export const byCode = (code: string) => REPLAYS.find((r) => r.code === code) ?? REPLAYS[0];
