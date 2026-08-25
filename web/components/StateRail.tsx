"use client";

import { motion } from "motion/react";

/* Where the call is, as the state machine sees it.
 *
 * This is the quietly important part of the widget. Every other voice demo
 * shows you a transcript; the transcript is the model talking, and a model
 * will happily say "you're booked" with nothing behind it. This rail is read
 * from the session's own state history, so a caller can watch the machine
 * move -- and the write only ever happens at one of these steps.
 *
 * Four labels, not the nine real states. `identify`, `research`, `validate`
 * and `repair` are engineering distinctions that mean nothing to a patient,
 * so they collapse into the step they serve. The full machine is in
 * docs/STATE_MACHINE.md and the deep section links there. */

const STEPS = [
  { key: "identify", label: "Understanding" },
  { key: "draft", label: "Finding a time" },
  { key: "approval", label: "Confirming" },
  { key: "wrap", label: "Booked" },
] as const;

const RANK: Record<string, number> = {
  intake: 0, identify: 1, research: 1,
  draft: 2, validate: 2, repair: 2,
  approval: 3, execute: 3, audit: 3,
  wrap: 4,
};

const BAD = new Set(["transfer", "refused", "failed", "abandoned"]);

export function StateRail({ states }: { states: string[] }) {
  if (states.length === 0) return null;

  const reached = Math.max(0, ...states.map((s) => RANK[s] ?? 0));
  const handed = states.some((s) => BAD.has(s));

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 border-t border-line2 px-4 py-2.5 sm:px-5">
      {STEPS.map((step, i) => {
        const done = reached > i;
        const now = reached === i + 1 && !handed;
        return (
          <span key={step.key} className="flex items-center gap-2">
            {i > 0 && <span className="h-px w-3 bg-line" aria-hidden />}
            <span
              className={`flex items-center gap-1.5 text-[12px] transition-colors ${
                done || now ? "text-ink2" : "text-ink3"
              }`}
            >
              <motion.span
                initial={false}
                animate={{ scale: now ? [1, 1.35, 1] : 1 }}
                transition={now ? { duration: 1.4, repeat: Infinity } : { duration: 0.2 }}
                className={`size-[6px] rounded-full ${
                  done ? "bg-brand" : now ? "bg-warm" : "bg-line"
                }`}
              />
              {step.label}
            </span>
          </span>
        );
      })}
      {handed && (
        <span className="ml-1 flex items-center gap-1.5 text-[12px] text-stop">
          <span className="size-[6px] rounded-full bg-stop" />
          Passed to a person
        </span>
      )}
    </div>
  );
}
