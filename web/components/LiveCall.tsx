"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { BackendDown, isAwake, sendTurn, startCall, ttsUrl } from "@/lib/api";
import { byCode, type Line } from "@/lib/replay";
import { Voice } from "@/lib/voice";
import { StateRail } from "./StateRail";
import { Waveform } from "./Waveform";

/* The hero widget.
 *
 * Three ways a visitor can meet this, and it has to be good in all three
 * because we do not get to choose which one they arrive in:
 *
 *   live      the Railway backend answers -- they type or talk to the real
 *             agent, the real state machine, the real clinical guard.
 *   replay    the container is cold, or there is no backend configured. It
 *             plays a recorded call instead and SAYS it is recorded. A hero
 *             showing a connection error is worse than one showing a
 *             recording; a recording pretending to be live is worse than
 *             both.
 *   reduced   prefers-reduced-motion. Same content, no typing animation.
 *
 * Typing is always available. Voice is the demo, but a phone in a quiet
 * waiting room, an in-app browser with no mic permission, or a laptop with
 * the mic switched off must all still be able to have the conversation. */

type Mode = "idle" | "connecting" | "live" | "replay";

const TYPE_MS = 16;

export function LiveCall() {
  const [mode, setMode] = useState<Mode>("idle");
  const [lines, setLines] = useState<Line[]>([]);
  const [states, setStates] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [lang, setLang] = useState("en-IN");
  const [booked, setBooked] = useState<string | null>(null);

  const callId = useRef<string | null>(null);
  const voice = useRef<Voice | null>(null);
  const tape = useRef<HTMLDivElement>(null);
  const cancelled = useRef(false);
  const reduced = useReducedMotion();

  useEffect(() => () => { cancelled.current = true; voice.current?.stop(); }, []);

  // Keep the newest turn in view without yanking the whole page around it.
  useEffect(() => {
    const el = tape.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines, busy]);

  const push = useCallback((line: Line) => setLines((prev) => [...prev, line]), []);

  /* -- the recorded path ------------------------------------------------- */

  const playReplay = useCallback(
    async (code: string) => {
      const replay = byCode(code);
      setLines([]);
      setStates([]);
      setMode("replay");
      for (const line of replay.lines) {
        if (cancelled.current) return;
        await new Promise((r) => setTimeout(r, reduced ? 120 : line.who === "agent" ? 620 : 900));
        if (cancelled.current) return;
        push(line);
        if (line.state) setStates((s) => [...s, line.state!]);
      }
    },
    [push, reduced],
  );

  /* -- the live path ----------------------------------------------------- */

  const begin = useCallback(async () => {
    // Unlock audio inside the tap itself. Anything later is blocked on iOS.
    voice.current = voice.current ?? new Voice();
    voice.current.unlock();

    cancelled.current = false;
    setMode("connecting");
    setNotice(null);
    setLines([]);
    setStates([]);
    setBooked(null);

    if (!(await isAwake())) {
      setNotice("The demo line is asleep — here's a recording of a real call instead.");
      void playReplay(lang);
      return;
    }

    try {
      const snap = await startCall(lang);
      callId.current = snap.call_id ?? null;
      setMode("live");
      push({ who: "agent", text: snap.reply, state: snap.state });
      setStates(snap.states ?? []);
      await speak(snap.clips, lang);
    } catch (err) {
      if (err instanceof BackendDown) {
        setNotice("Couldn't reach the demo line — here's a recording of a real call instead.");
        void playReplay(lang);
      } else {
        setNotice(err instanceof Error ? err.message : "Something went wrong.");
        setMode("idle");
      }
    }
  }, [lang, playReplay, push]);

  const speak = useCallback(async (clips: string[], language: string) => {
    if (!voice.current?.ready || clips.length === 0) return;
    setSpeaking(true);
    try {
      await voice.current.say(clips.map((c) => ttsUrl(c, language)));
    } finally {
      setSpeaking(false);
    }
  }, []);

  const send = useCallback(
    async (text: string) => {
      const said = text.trim();
      if (!said || busy) return;
      setDraft("");
      push({ who: "caller", text: said });

      if (mode !== "live" || !callId.current) {
        // Recorded mode: be honest rather than faking an answer.
        push({
          who: "agent",
          text: "This is a recording, so I can't answer that one. Wake the live line with “Start a call” to talk to the real agent.",
        });
        return;
      }

      setBusy(true);
      voice.current?.stop();
      try {
        const snap = await sendTurn(callId.current, said, lang);
        if (cancelled.current) return;
        if (snap.language) setLang(snap.language);
        push({
          who: "agent",
          text: snap.reply,
          tools: snap.tools?.map((t) => t.name),
          state: snap.state,
        });
        setStates(snap.states ?? []);
        if (snap.appointments?.length) {
          const a = snap.appointments[snap.appointments.length - 1];
          setBooked(`${a.doctor} · ${a.specialty} · ${a.starts_at}`);
        }
        setBusy(false);
        await speak(snap.clips, snap.language ?? lang);
        if (snap.terminal) {
          setNotice("That call has ended. Start another whenever you like.");
          setMode("idle");
          callId.current = null;
        }
      } catch (err) {
        setBusy(false);
        setNotice(
          err instanceof BackendDown
            ? "Lost the line to the demo backend."
            : err instanceof Error
              ? err.message
              : "Something went wrong.",
        );
      }
    },
    [busy, lang, mode, push, speak],
  );

  const hangUp = useCallback(() => {
    cancelled.current = true;
    voice.current?.stop();
    setMode("idle");
    setBusy(false);
    setSpeaking(false);
    callId.current = null;
  }, []);

  const idle = mode === "idle";

  return (
    <div className="card overflow-hidden" data-tint>
      {/* -- header -------------------------------------------------------- */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line2 px-4 py-3 sm:px-5">
        <span className="flex items-center gap-2">
          <span
            className={`size-[7px] shrink-0 rounded-full transition-colors ${
              mode === "live"
                ? "bg-brand shadow-[0_0_0_3px_var(--c-brandsoft)]"
                : mode === "replay"
                  ? "bg-warm shadow-[0_0_0_3px_var(--c-warmsoft)]"
                  : "bg-ink3"
            }`}
          />
          <span className="eyebrow !text-ink2">
            {mode === "live" ? "Live" : mode === "replay" ? "Recording" : mode === "connecting" ? "Connecting" : "Ready"}
          </span>
        </span>

        <div className="ml-auto flex items-center gap-1" role="group" aria-label="Language">
          {["en-IN", "ta-IN", "hi-IN"].map((code) => {
            const r = byCode(code);
            return (
              <button
                key={code}
                type="button"
                onClick={() => {
                  setLang(code);
                  if (mode === "replay") void playReplay(code);
                }}
                aria-pressed={lang === code}
                className={`min-h-9 rounded-full px-3 text-[13px] transition-colors ${
                  lang === code
                    ? "bg-brandsoft text-brand"
                    : "text-ink3 hover:text-ink2"
                } ${r.indic ? "indic" : ""}`}
              >
                {r.native}
              </button>
            );
          })}
        </div>
      </div>

      {/* -- tape ---------------------------------------------------------- */}
      <div
        ref={tape}
        className="scroller flex h-[300px] flex-col gap-3 overflow-y-auto px-4 py-4 sm:h-[340px] sm:px-5"
        aria-live="polite"
        aria-atomic="false"
      >
        {lines.length === 0 && idle && (
          <div className="m-auto max-w-[30ch] text-center">
            <p className="serif text-[22px] leading-tight text-ink sm:text-[25px]">
              Call the front desk.
            </p>
            <p className="mt-2 text-[14px] text-ink2">
              Speak or type — English, Tamil or Hindi, and you can switch mid-sentence.
            </p>
          </div>
        )}

        <AnimatePresence initial={false}>
          {lines.map((line, i) => (
            <Turn key={i} line={line} typing={!reduced && line.who === "agent"} />
          ))}
        </AnimatePresence>

        {busy && <Thinking />}
      </div>

      {/* -- booked ribbon -------------------------------------------------- */}
      <AnimatePresence>
        {booked && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden border-t border-line2 bg-brandsoft"
          >
            <p className="px-4 py-2.5 text-[13px] text-brand sm:px-5">
              <span className="eyebrow !text-brand">Booked</span>{" "}
              <span className="ml-2 text-ink2">{booked}</span>
            </p>
          </motion.div>
        )}
      </AnimatePresence>

      <StateRail states={states} />

      {/* -- notice --------------------------------------------------------- */}
      <AnimatePresence>
        {notice && (
          <motion.p
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden border-t border-line2 px-4 py-2.5 text-[13px] text-ink2 sm:px-5"
          >
            {notice}
          </motion.p>
        )}
      </AnimatePresence>

      {/* -- controls ------------------------------------------------------- */}
      <div className="border-t border-line2 px-4 py-3 sm:px-5">
        <div className="flex items-center gap-3">
          {idle || mode === "replay" ? (
            <button
              type="button"
              onClick={begin}
              className="inline-flex min-h-11 items-center gap-2.5 rounded-full bg-brand px-5 text-[15px] font-medium text-brandink shadow-[var(--shadow-lift)] transition-transform active:scale-[0.98]"
            >
              <PhoneIcon />
              {mode === "replay" ? "Try it live" : "Start a call"}
            </button>
          ) : (
            <button
              type="button"
              onClick={hangUp}
              className="inline-flex min-h-11 items-center gap-2 rounded-full bg-stop px-5 text-[15px] font-medium text-white transition-transform active:scale-[0.98]"
            >
              End call
            </button>
          )}

          <Waveform active={speaking || busy} />
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(draft);
          }}
          className="mt-3 flex gap-2"
        >
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={idle ? "Start a call, then type here…" : "Type instead of talking…"}
            aria-label="Type a message to the front desk"
            enterKeyHint="send"
            autoComplete="off"
            className="min-h-11 w-full rounded-xl border border-line bg-bg px-3.5 text-[15px] text-ink outline-none placeholder:text-ink3 focus:border-brand"
          />
          <button
            type="submit"
            disabled={!draft.trim() || busy}
            className="min-h-11 shrink-0 rounded-xl border border-line px-4 text-[14px] font-medium text-ink2 transition-colors enabled:hover:border-brand enabled:hover:text-brand disabled:opacity-40"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}

/* -- one turn ------------------------------------------------------------ */

function Turn({ line, typing }: { line: Line; typing: boolean }) {
  const indic = /[஀-௿ऀ-ॿ]/.test(line.text);
  const shown = useTyped(line.text, typing && !line.hold);
  const mine = line.who === "caller";

  return (
    <motion.div
      layout="position"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: line.hold ? 0.5 : 1, y: 0 }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}
      className={`flex max-w-[88%] flex-col gap-1 ${mine ? "self-end items-end" : "self-start"}`}
    >
      <span className="eyebrow">{mine ? "You" : "Front desk"}</span>
      <p
        className={`text-[15px] leading-[1.5] ${indic ? "indic" : ""} ${
          mine
            ? "rounded-2xl rounded-br-sm bg-line2 px-3.5 py-2 text-ink2"
            : "text-ink"
        }`}
      >
        {shown}
      </p>
      {line.tools && line.tools.length > 0 && (
        <span className="mono text-[11px] text-ink3">
          {line.tools.map((t) => `${t}()`).join("  ")}
        </span>
      )}
    </motion.div>
  );
}

/** Types an agent line out rather than snapping it in.
 *
 *  Not decoration: the desk speaks its reply, and text that appears all at
 *  once finishes long before the voice does, which reads as the caption
 *  being out of sync with the audio. Roughly matches speaking pace. */
function useTyped(text: string, on: boolean) {
  const [n, setN] = useState(on ? 0 : text.length);
  useEffect(() => {
    if (!on) {
      setN(text.length);
      return;
    }
    setN(0);
    let i = 0;
    const id = setInterval(() => {
      i += 1;
      setN(i);
      if (i >= text.length) clearInterval(id);
    }, TYPE_MS);
    return () => clearInterval(id);
  }, [text, on]);
  return text.slice(0, n);
}

function Thinking() {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="flex items-center gap-1.5 self-start px-1"
      aria-label="The front desk is working"
    >
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="size-1.5 rounded-full bg-ink3"
          animate={{ opacity: [0.25, 1, 0.25] }}
          transition={{ duration: 1.1, repeat: Infinity, delay: i * 0.16 }}
        />
      ))}
    </motion.div>
  );
}

function PhoneIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-[18px] fill-current" aria-hidden>
      <path d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.2.4 2.4.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.4 0 .7-.2 1l-2.3 2.2Z" />
    </svg>
  );
}
