"use client";

import { Reveal } from "./Reveal";

/* Back to paper.
 *
 * The bookend is doing work: the page opened as a clinic, argued as an
 * engineering document, and closes as a clinic again. Whoever we are asking
 * to act here is a person who runs a practice, not the person who read the
 * bento grid. */

export function Closing() {
  return (
    <section className="px-4 pt-24 pb-16 sm:px-6 lg:pt-32">
      <div className="mx-auto max-w-3xl text-center">
        <Reveal>
          <span className="eyebrow">The ask</span>
        </Reveal>
        <Reveal delay={0.05}>
          <h2 className="serif mt-4 text-[38px] leading-[1.08] text-ink sm:text-[50px]">
            Ring it yourself.
          </h2>
        </Reveal>
        <Reveal delay={0.1}>
          <p className="mx-auto mt-6 max-w-[44ch] text-[17px] leading-[1.6] text-ink2">
            The demo above is the real agent on a demo clinic. Book something,
            change your mind halfway, talk over it, switch to Tamil. It holds up
            or it doesn&rsquo;t — that&rsquo;s the whole pitch.
          </p>
        </Reveal>
        <Reveal delay={0.16}>
          <div className="mt-9 flex flex-wrap items-center justify-center gap-x-5 gap-y-3">
            <a
              href="#call"
              className="inline-flex min-h-12 items-center gap-2.5 rounded-full bg-brand px-7 text-[16px] font-medium text-brandink shadow-[var(--shadow-lift)] transition-transform active:scale-[0.98]"
            >
              Start a call
            </a>
            <a
              href="mailto:hello@buteforce.com?subject=Voice%20Desk"
              className="min-h-12 content-center text-[15px] text-ink2 underline-offset-4 hover:underline"
            >
              Talk to us about your clinic
            </a>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

export function Footer() {
  return (
    <footer
      className="px-4 pb-10 sm:px-6"
      style={{ paddingBottom: "max(2.5rem, env(safe-area-inset-bottom))" }}
    >
      <div className="mx-auto max-w-6xl border-t border-line pt-8">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[13px] text-ink3">
            Voice Desk — a Buteforce project. Demo clinic, fictional doctors, no
            real patient data.
          </p>
          <p className="mono text-[11.5px] text-ink3">
            Answers in ta-IN · hi-IN · en-IN
          </p>
        </div>
      </div>
    </footer>
  );
}
