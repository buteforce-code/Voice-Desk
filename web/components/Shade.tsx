"use client";

import { useEffect, useRef } from "react";

/* The paper-to-ink shift.
 *
 * An IntersectionObserver rather than a scroll handler, deliberately: a
 * scroll listener runs on every frame of every scroll for one boolean, and
 * on a mid-range Android that is the jank a visitor blames on the page being
 * heavy. This fires twice per page.
 *
 * The margins carve a band across the middle of the viewport. The zone turns
 * the page to ink when it reaches that band and back to paper when it leaves,
 * so the change lands while the reader is looking at the middle of the
 * screen rather than at the very top or bottom edge. */
export function ShadeZone({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const root = document.documentElement;
    const io = new IntersectionObserver(
      ([entry]) => {
        root.dataset.shade = entry.isIntersecting ? "ink" : "paper";
      },
      { rootMargin: "-45% 0px -45% 0px", threshold: 0 },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      // Leaving the page mid-zone must not strand the next mount in the dark.
      root.dataset.shade = "paper";
    };
  }, []);

  return (
    <div ref={ref} data-tint>
      {children}
    </div>
  );
}
