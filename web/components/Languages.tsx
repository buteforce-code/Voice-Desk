"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { REPLAYS } from "@/lib/replay";
import { Reveal } from "./Reveal";

/* The language section.
 *
 * The claim is not "supports 55 languages" -- everyone says that, and it is
 * measured on clean single-language audio that nobody in Chennai produces. The
 * claim is code-switching: "நாளைக்கு morning ஒரு appointment வேணும்" is one
 * sentence in two languages and it is how people actually book.
 *
 * Every transcript here is a real capture, which is why the caller's lines
 * look the way they do rather than like textbook Tamil. */

export function Languages() {
  const [active, setActive] = useState(1); // Tamil first: it is the harder claim.
  const replay = REPLAYS[active];

  return (
    <section id="languages" className="scroll-mt-20 px-4 py-24 sm:px-6 lg:py-28">
      <div className="mx-auto max-w-6xl">
        <div className="grid gap-10 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)] lg:gap-16">
          <div>
            <Reveal>
              <span className="eyebrow">One sentence, two languages</span>
            </Reveal>
            <Reveal delay={0.05}>
              <h2 className="serif mt-4 text-[36px] leading-[1.08] text-ink sm:text-[46px]">
                Nobody books an appointment in textbook Tamil.
              </h2>
            </Reveal>
            <Reveal delay={0.1}>
              <p className="mt-6 max-w-[46ch] text-[16.5px] leading-[1.6] text-ink2">
                Real callers switch mid-sentence — Tamil grammar, English nouns,
                a doctor&rsquo;s name in either. Most voice stacks are English-first
                and quietly force the caller into the nearest language they know.
                This one hears the mix and answers in it.
              </p>
            </Reveal>
            <Reveal delay={0.15}>
              <p className="mt-5 max-w-[46ch] text-[14px] leading-relaxed text-ink3">
                Speech and voice are Sarvam&rsquo;s Saaras and Bulbul, chosen because
                they are built for Indic code-mixing rather than adapted to it.
              </p>
            </Reveal>
          </div>

          <Reveal delay={0.08} y={18}>
            <div className="card overflow-hidden">
              <div
                className="flex gap-1 border-b border-line2 p-2"
                role="tablist"
                aria-label="Language"
              >
                {REPLAYS.map((r, i) => (
                  <button
                    key={r.code}
                    role="tab"
                    aria-selected={i === active}
                    onClick={() => setActive(i)}
                    className={`relative min-h-10 flex-1 rounded-lg px-3 text-[14px] transition-colors ${
                      i === active ? "text-brand" : "text-ink3 hover:text-ink2"
                    } ${r.indic ? "indic" : ""}`}
                  >
                    {i === active && (
                      <motion.span
                        layoutId="lang-pill"
                        className="absolute inset-0 rounded-lg bg-brandsoft"
                        transition={{ type: "spring", stiffness: 420, damping: 36 }}
                      />
                    )}
                    <span className="relative">{r.native}</span>
                  </button>
                ))}
              </div>

              <div className="scroller max-h-[380px] min-h-[300px] overflow-y-auto px-5 py-5">
                <AnimatePresence mode="wait">
                  <motion.div
                    key={replay.code}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -8 }}
                    transition={{ duration: 0.22 }}
                    className="flex flex-col gap-3.5"
                  >
                    {replay.lines.map((line, i) => (
                      <div
                        key={i}
                        className={`flex max-w-[90%] flex-col gap-1 ${
                          line.who === "caller" ? "items-end self-end" : "self-start"
                        } ${line.hold ? "opacity-50" : ""}`}
                      >
                        <span className="eyebrow">
                          {line.who === "caller" ? "Caller" : "Front desk"}
                        </span>
                        <p
                          className={`text-[15px] leading-[1.55] ${replay.indic ? "indic" : ""} ${
                            line.who === "caller"
                              ? "rounded-2xl rounded-br-sm bg-line2 px-3.5 py-2 text-ink2"
                              : "text-ink"
                          }`}
                        >
                          {line.text}
                        </p>
                        {line.tools && (
                          <span className="mono text-[11px] text-ink3">
                            {line.tools.map((t) => `${t}()`).join("  ")}
                          </span>
                        )}
                      </div>
                    ))}
                  </motion.div>
                </AnimatePresence>
              </div>

              <p className="border-t border-line2 px-5 py-3 text-[12px] text-ink3">
                Recorded from the live agent, 25 August 2026.
              </p>
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
