"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useState } from "react";
import { LiveCall } from "./LiveCall";
import { Reveal } from "./Reveal";

/* The hero.
 *
 * Asymmetric on purpose: the headline column is narrower than the demo, and
 * the demo overlaps the baseline grid. A centred headline over a centred card
 * is the shape of every AI landing page shipped this year, and the product it
 * is selling here is not a chatbot -- it is a front desk, which is a specific
 * job with a specific feeling.
 *
 * On a phone the two stack and the DEMO comes first below the headline,
 * because a visitor who arrives on a phone came to hear it, not to read. */

const WORDS = ["never misses a call.", "answers in Tamil.", "books while you sleep."];

export function Hero() {
  const reduced = useReducedMotion();

  return (
    <section
      id="top"
      // Three blocks, and the ORDER differs by device on purpose. On a phone
      // the demo comes straight after the headline: someone who opened this on
      // a phone came to hear the thing, and making them scroll past a stats
      // row to reach it is how a demo goes untried. On a wide screen the
      // stats tuck under the headline and the demo sits beside both.
      className="mx-auto grid max-w-6xl gap-10 px-4 pt-10 pb-16 sm:px-6 md:pt-16 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1fr)] lg:grid-rows-[auto_auto] lg:gap-x-14 lg:gap-y-8 lg:pt-24"
    >
      <div className="flex flex-col justify-center lg:col-start-1 lg:row-start-1">
        <Reveal>
          <span className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1.5 text-[12.5px] text-ink2">
            <span className="size-[6px] rounded-full bg-brand" />
            Built for Indian clinics
          </span>
        </Reveal>

        <Reveal delay={0.06}>
          <h1 className="serif mt-5 text-[40px] leading-[1.06] text-ink sm:text-[54px] lg:text-[62px]">
            The front desk that{" "}
            <span className="relative inline-block">
              <Cycler words={WORDS} still={reduced ?? false} />
            </span>
          </h1>
        </Reveal>

        <Reveal delay={0.12}>
          <p className="mt-6 max-w-[46ch] text-[17px] leading-[1.6] text-ink2">
            An appointment line for clinics that answers every call, speaks the
            language the patient speaks, and writes into the real diary. What it
            is not allowed to do is enforced by code that isn&rsquo;t there —
            not by asking a model nicely.
          </p>
        </Reveal>

        <Reveal delay={0.18}>
          <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-3">
            <a
              href="#call"
              className="inline-flex min-h-12 items-center gap-2.5 rounded-full bg-brand px-6 text-[15.5px] font-medium text-brandink shadow-[var(--shadow-lift)] transition-transform active:scale-[0.98]"
            >
              Hear it answer
            </a>
            <a href="#controls" className="text-[15px] text-ink2 underline-offset-4 hover:underline">
              How it&rsquo;s held back →
            </a>
          </div>
        </Reveal>

      </div>

      <Reveal delay={0.1} y={20} className="lg:col-start-2 lg:row-span-2 lg:row-start-1 lg:pt-2" as="div">
        <div id="call" className="scroll-mt-24">
          <LiveCall />
          <p className="mt-3 px-1 text-[12.5px] leading-relaxed text-ink3">
            A real agent on a demo clinic, not a script. Nothing you type is
            stored beyond the call, and no real patient exists in this diary.
          </p>
        </div>
      </Reveal>

      <Reveal delay={0.24} className="lg:col-start-1 lg:row-start-2">
        <dl className="flex flex-wrap gap-x-8 gap-y-5 border-t border-line pt-6">
          {[
            ["3", "languages, code-switched"],
            ["1", "state where a write can happen"],
            ["0", "ways to place an outbound call"],
          ].map(([n, label]) => (
            <div key={label} className="min-w-[8rem]">
              <dt className="serif tnum text-[30px] leading-none text-brand">{n}</dt>
              <dd className="mt-1.5 max-w-[16ch] text-[13px] leading-snug text-ink3">{label}</dd>
            </div>
          ))}
        </dl>
      </Reveal>
    </section>
  );
}

/* The headline's second half, cycling.
 *
 * Three claims that are each true and each aimed at a different reader -- a
 * clinic owner hears the first, a Chennai patient the second, an operator the
 * third. Held long enough to read, and frozen entirely under
 * prefers-reduced-motion, where moving text is the exact thing being opted
 * out of. */
function Cycler({ words, still }: { words: string[]; still: boolean }) {
  const [i, setI] = useState(0);

  useEffect(() => {
    if (still) return;
    const id = setInterval(() => setI((n) => (n + 1) % words.length), 3200);
    return () => clearInterval(id);
  }, [still, words.length]);

  if (still) return <span className="text-brand">{words[0]}</span>;

  return (
    <span className="inline-grid">
      {/* One phrase at a time, swapped by index.
          An earlier version staggered every phrase on a single shared
          timeline using `times`. Motion needs that array to start at 0, and
          offsetting it per phrase meant the keyframes were undefined for most
          of the cycle -- the headline rendered a blank gap roughly 90% of the
          time. Cheap trick, expensive bug: it only shows up on screen. */}
      {/* Crossfade, NOT `mode="wait"`.
          Waiting runs the exit to completion before the entry starts, which
          leaves a gap with no text in it -- about a fifth of every cycle,
          and the headline is the first thing anyone sees. Overlapping them
          in one grid cell means a phrase is always on screen. */}
      <AnimatePresence>
        <motion.span
          key={i}
          className="col-start-1 row-start-1 whitespace-nowrap text-brand"
          initial={{ opacity: 0, y: "0.3em" }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: "-0.3em" }}
          transition={{ duration: 0.42, ease: [0.16, 1, 0.3, 1] }}
        >
          {words[i]}
        </motion.span>
      </AnimatePresence>
      {/* Reserves the width of the longest phrase so the headline never reflows. */}
      <span className="invisible col-start-1 row-start-1 whitespace-nowrap" aria-hidden>
        {words.reduce((a, b) => (a.length > b.length ? a : b))}
      </span>
    </span>
  );
}
