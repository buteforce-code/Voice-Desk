"use client";

import { motion, useReducedMotion } from "motion/react";

/* Entry only, once, on a short spring.
 *
 * A clinic page should feel calm. Nothing here loops, nothing re-animates on
 * the way back up, and `once` matters more than it looks -- a section that
 * re-plays every time it re-enters the viewport turns an ordinary scroll back
 * to the top into a light show. */
export function Reveal({
  children,
  delay = 0,
  y = 14,
  className,
  as = "div",
}: {
  children: React.ReactNode;
  delay?: number;
  y?: number;
  className?: string;
  as?: "div" | "section" | "li" | "p" | "h2";
}) {
  const reduced = useReducedMotion();
  const M = motion[as];

  return (
    <M
      className={className}
      initial={reduced ? false : { opacity: 0, y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "0px 0px -12% 0px" }}
      transition={{ type: "spring", stiffness: 320, damping: 34, delay }}
    >
      {children}
    </M>
  );
}
