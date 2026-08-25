"use client";

import { motion, useMotionValueEvent, useScroll } from "motion/react";
import { useState } from "react";

const LINKS = [
  { href: "#call", label: "The call" },
  { href: "#languages", label: "Languages" },
  { href: "#controls", label: "Controls" },
  { href: "#stack", label: "Stack" },
];

export function Nav() {
  const [stuck, setStuck] = useState(false);
  const { scrollY } = useScroll();
  useMotionValueEvent(scrollY, "change", (v) => setStuck(v > 24));

  return (
    <motion.header
      // The safe-area inset keeps the bar clear of an iPhone's notch in
      // landscape, where a plain top padding puts the wordmark under it.
      style={{ paddingLeft: "max(1rem, env(safe-area-inset-left))", paddingRight: "max(1rem, env(safe-area-inset-right))" }}
      className={`sticky top-0 z-50 transition-colors duration-500 ${
        stuck ? "border-b border-line bg-bg/85 backdrop-blur-xl" : "border-b border-transparent"
      }`}
      data-tint
    >
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-3">
        <a href="#top" className="flex items-center gap-2.5" aria-label="Voice Desk, home">
          <span className="grid size-8 shrink-0 place-items-center rounded-[9px] bg-brand text-brandink">
            <svg viewBox="0 0 24 24" className="size-4 fill-current" aria-hidden>
              <path d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.2.4 2.4.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.4 0 .7-.2 1l-2.3 2.2Z" />
            </svg>
          </span>
          <span className="font-semibold tracking-[-0.015em]">Voice Desk</span>
        </a>

        <nav className="ml-auto hidden items-center gap-1 md:flex" aria-label="Sections">
          {LINKS.map((l) => (
            <a
              key={l.href}
              href={l.href}
              className="rounded-full px-3 py-2 text-[14px] text-ink2 transition-colors hover:text-ink"
            >
              {l.label}
            </a>
          ))}
        </nav>

        <a
          href="#call"
          className="ml-auto inline-flex min-h-10 items-center rounded-full border border-line px-4 text-[14px] font-medium text-ink transition-colors hover:border-brand hover:text-brand md:ml-2"
        >
          Try it
        </a>
      </div>
    </motion.header>
  );
}
