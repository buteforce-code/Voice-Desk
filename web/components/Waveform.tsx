"use client";

import { motion, useReducedMotion } from "motion/react";

/* Twelve bars that mean something.
 *
 * Not a decorative equaliser: it is the only thing on the widget that tells a
 * visitor whether the line is alive while nothing is being said. It is bell-
 * weighted from the centre so it reads as a voice rather than a spectrum, and
 * flat when idle -- a waveform that dances with nothing playing is the sort
 * of detail that makes the rest of the page look staged. */

const BARS = 12;
const weight = (i: number) => 1 - Math.abs(i - (BARS - 1) / 2) / (BARS * 0.72);

export function Waveform({ active }: { active: boolean }) {
  const reduced = useReducedMotion();

  return (
    <div
      className="flex h-6 items-center gap-[3px]"
      aria-hidden
    >
      {Array.from({ length: BARS }, (_, i) => {
        const w = weight(i);
        const peak = 5 + w * 17;
        return (
          <motion.span
            key={i}
            className="w-[3px] rounded-full bg-brand"
            initial={false}
            animate={
              active && !reduced
                ? { height: [4, peak, 6 + w * 8, peak * 0.8, 4], opacity: 0.55 + w * 0.45 }
                : { height: 4, opacity: 0.28 }
            }
            transition={
              active && !reduced
                ? {
                    duration: 0.9 + w * 0.5,
                    repeat: Infinity,
                    ease: "easeInOut",
                    delay: i * 0.045,
                  }
                : { duration: 0.25 }
            }
          />
        );
      })}
    </div>
  );
}
