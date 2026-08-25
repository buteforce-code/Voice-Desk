"use client";

import { motion } from "motion/react";
import { CLINICAL_TURN } from "@/lib/replay";
import { Reveal } from "./Reveal";

/* The ink half: what holds the thing up.
 *
 * This is the section the product actually competes on. Anyone can put a
 * model on a phone line in an afternoon; the reason this one can be pointed
 * at a clinic is that the things it must never do are absent from the code
 * rather than discouraged in a prompt. The palette inverts here because the
 * subject changes -- the reader has stopped being a patient and started
 * being whoever has to sign off on it. */

const CONTROLS = [
  {
    n: "01",
    title: "It cannot give medical advice",
    body: "Every sentence is screened by a classifier after the model writes it and before the caller hears it. Not an instruction in a prompt — a step in the path that can replace the whole utterance and hand the call to a person.",
    code: "guard_agent_turn(spoken)  →  blocked · transfer",
    tone: "stop",
  },
  {
    n: "02",
    title: "It cannot call anyone",
    body: "Outbound calling is prohibited under TRAI's TCCCPR, so there is no function that dials. CI greps for one on every push and fails the build if it ever appears.",
    code: "grep -rE '(outbound_call|dial)\\(' src/  →  exit 1",
    tone: "warm",
  },
  {
    n: "03",
    title: "It cannot book without a yes",
    body: "Consent is matched in code from the caller's own words, in three languages, with negation dominating. The model can ask to write; only the state machine mints the token that lets it.",
    code: "draft → validate → approval → execute",
    tone: "brand",
  },
  {
    n: "04",
    title: "It cannot see another patient",
    body: "Lookup is identity-gated server-side, and the tool that finds appointments has no field for someone else's number. A model deciding it may call a tool is not authorization.",
    code: "FindAppointmentsIn { }  ·  no phone field",
    tone: "brand",
  },
] as const;

export function Controls() {
  return (
    <section id="controls" className="scroll-mt-20 px-4 py-24 sm:px-6 lg:py-32">
      <div className="mx-auto max-w-6xl">
        <Reveal>
          <span className="eyebrow">The part that matters</span>
        </Reveal>

        <Reveal delay={0.05}>
          <h2 className="serif mt-4 max-w-[16ch] text-[38px] leading-[1.08] text-ink sm:text-[50px] lg:text-[58px]">
            A prompt is not a control.
          </h2>
        </Reveal>

        <Reveal delay={0.1}>
          <p className="mt-6 max-w-[54ch] text-[17px] leading-[1.6] text-ink2">
            Most voice agents are held together by a paragraph of instructions
            asking the model to behave. That works until the call it doesn&rsquo;t.
            Here, the four things this line must never do are enforced by missing
            code paths, missing database grants, and one classifier standing
            between the model and the speaker.
          </p>
        </Reveal>

        {/* Bento: the first card is wide because the clinical guard is the one
            that would end the company, and a four-up grid of equal squares
            would say all four are equally important. */}
        <ul className="mt-14 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {CONTROLS.map((c, i) => (
            <Reveal
              as="li"
              key={c.n}
              delay={0.04 * i}
              // `min-w-0` is load-bearing, not tidiness. A grid item defaults
              // to `min-width: auto`, so it refuses to shrink below the
              // intrinsic width of its widest child -- here a `whitespace-pre`
              // code line. The card grew past its column, the page scrolled
              // sideways on every phone, and the `overflow-x-auto` below never
              // got the chance to do its job.
              className={`card flex min-w-0 flex-col p-6 ${i === 0 ? "lg:col-span-2" : ""}`}
            >
              <div className="flex items-baseline gap-3">
                <span className="mono text-[12px] text-ink3">{c.n}</span>
                <span
                  className={`h-px flex-1 ${
                    c.tone === "stop" ? "bg-stop/40" : c.tone === "warm" ? "bg-warm/40" : "bg-brand/40"
                  }`}
                />
              </div>
              <h3 className="serif mt-4 text-[23px] leading-tight text-ink">{c.title}</h3>
              <p className="mt-3 flex-1 text-[15px] leading-[1.6] text-ink2">{c.body}</p>
              <code className="scroller mono mt-5 block min-w-0 overflow-x-auto rounded-lg border border-line bg-bg px-3 py-2.5 text-[12px] whitespace-pre text-ink3">
                {c.code}
              </code>
            </Reveal>
          ))}
        </ul>

        <Refusal />
      </div>
    </section>
  );
}

/* The refusal, shown as a real exchange.
 *
 * The interesting thing is not that it declines -- anything declines. It is
 * that it declines the ONE clinical inch and keeps the rest of the call
 * moving. A line that answers "I can't advise on your headache" to someone
 * who only said why they were ringing has answered a question nobody put, and
 * that is the commonest way these things become useless. */
function Refusal() {
  return (
    <Reveal delay={0.08}>
      <div className="mt-16 grid gap-8 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:items-center">
        <div>
          <span className="eyebrow">Where the line sits</span>
          <h3 className="serif mt-3 text-[28px] leading-tight text-ink sm:text-[32px]">
            Refusing well is harder than refusing.
          </h3>
          <p className="mt-4 max-w-[46ch] text-[15.5px] leading-[1.6] text-ink2">
            A caller who mentions a symptom is not asking for a diagnosis —
            they&rsquo;re saying why they rang. Matching a specialty to a symptom
            would be triage, and that transfers. Booking one the caller names is
            just the job.
          </p>
        </div>

        <div className="card overflow-hidden">
          <div className="flex items-center gap-2 border-b border-line2 px-5 py-3">
            <span className="size-[7px] rounded-full bg-warm" />
            <span className="eyebrow">Real turn · guard passed</span>
          </div>
          <div className="flex flex-col gap-3.5 px-5 py-5">
            {CLINICAL_TURN.map((line, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, x: line.who === "caller" ? 10 : -10 }}
                whileInView={{ opacity: 1, x: 0 }}
                viewport={{ once: true }}
                transition={{ delay: 0.12 * i, type: "spring", stiffness: 300, damping: 30 }}
                className={`flex max-w-[92%] flex-col gap-1 ${
                  line.who === "caller" ? "items-end self-end" : "self-start"
                }`}
              >
                <span className="eyebrow">{line.who === "caller" ? "Caller" : "Front desk"}</span>
                <p
                  className={`text-[15px] leading-[1.5] ${
                    line.who === "caller"
                      ? "rounded-2xl rounded-br-sm bg-line2 px-3.5 py-2 text-ink2"
                      : "text-ink"
                  }`}
                >
                  {line.text}
                </p>
              </motion.div>
            ))}
          </div>
        </div>
      </div>
    </Reveal>
  );
}
