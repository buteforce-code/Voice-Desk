"use client";

import { Reveal } from "./Reveal";

/* The stack, and the residency argument that comes with it.
 *
 * Kept in the ink half because this is the slide a technical buyer or an
 * investor asks for, and it is also where the moat is: caller audio and
 * patient rows never leave India, which is a DPDP requirement that most of
 * the hosted voice platforms cannot meet at any price today. */

const LAYERS = [
  ["Telephony", "Plivo · Exotel", "Inbound DID only. There is no outbound path."],
  ["Speech", "Sarvam Saaras · Bulbul", "Built for Indic code-mixing, not adapted to it."],
  ["Reasoning", "Gemini · OpenRouter", "A seam, not a preference — either provider, swapped by config."],
  ["Orchestration", "Pipecat, self-hosted", "Ours to instrument. Turn-taking and barge-in are not a vendor's opinion."],
  ["Data", "Supabase · Mumbai", "ap-south-1. Non-negotiable, and enforced at startup."],
  ["Deploy", "Railway · Asia SE", "Never a laptop."],
] as const;

export function Stack() {
  return (
    <section id="stack" className="scroll-mt-20 px-4 pb-28 sm:px-6">
      <div className="mx-auto max-w-6xl">
        <div className="grid gap-10 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.05fr)] lg:gap-16">
          <div>
            <Reveal>
              <span className="eyebrow">Where it runs</span>
            </Reveal>
            <Reveal delay={0.05}>
              <h2 className="serif mt-4 text-[34px] leading-[1.1] text-ink sm:text-[42px]">
                Every word a patient says stays in India.
              </h2>
            </Reveal>
            <Reveal delay={0.1}>
              <p className="mt-6 max-w-[44ch] text-[16px] leading-[1.6] text-ink2">
                India&rsquo;s DPDP Act is not a checkbox on this product — it decided
                the architecture. Data at rest is Supabase Mumbai and the service
                refuses to start anywhere else. The hosted voice platforms that
                sound closest to this have no India region at all.
              </p>
            </Reveal>
            <Reveal delay={0.15}>
              <div className="mt-8 flex flex-wrap gap-2">
                {["DPDP", "TRAI TCCCPR", "AI disclosure, first turn", "Audit row per write"].map((t) => (
                  <span
                    key={t}
                    className="rounded-full border border-line px-3 py-1.5 text-[12.5px] text-ink2"
                  >
                    {t}
                  </span>
                ))}
              </div>
            </Reveal>
          </div>

          <Reveal delay={0.08}>
            <ul className="card divide-y divide-line2 overflow-hidden">
              {LAYERS.map(([layer, choice, why]) => (
                <li key={layer} className="grid gap-1 px-5 py-4 sm:grid-cols-[7.5rem_1fr] sm:gap-4">
                  <span className="eyebrow pt-1">{layer}</span>
                  <div>
                    <p className="text-[15px] font-medium text-ink">{choice}</p>
                    <p className="mt-0.5 text-[13.5px] leading-snug text-ink3">{why}</p>
                  </div>
                </li>
              ))}
            </ul>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
