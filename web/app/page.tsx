import { Closing, Footer } from "@/components/Closing";
import { Controls } from "@/components/Controls";
import { Hero } from "@/components/Hero";
import { Languages } from "@/components/Languages";
import { Nav } from "@/components/Nav";
import { ShadeZone } from "@/components/Shade";
import { Stack } from "@/components/Stack";

/* The page, in three acts.
 *
 *   paper   the clinic. What a patient hears.
 *   ink     the engineering. What stops it saying the wrong thing.
 *   paper   the ask, back in the clinic's voice.
 *
 * `ShadeZone` wraps the middle act and flips the palette when it reaches the
 * middle of the viewport. The inversion is the argument, not an effect: the
 * reader stops being a patient somewhere around "A prompt is not a control",
 * and the page should look like it knows that. */

export default function Page() {
  return (
    <>
      <Nav />
      <main>
        <Hero />
        <Languages />

        <ShadeZone>
          <Controls />
          <Stack />
        </ShadeZone>

        <Closing />
      </main>
      <Footer />
    </>
  );
}
