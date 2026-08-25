/* Playing the desk's replies in a browser, including the awkward ones.
 *
 * **One audio element for the whole call, unlocked inside the click.** iOS
 * Safari only lets audio start from a user gesture, and every clip after the
 * first starts from a fetch callback instead -- which is a different task and
 * is blocked. Creating a fresh `new Audio()` per clip therefore works on a
 * laptop and is silent on half the phones that will ever see this page. The
 * fix is to unlock ONE element during the tap that starts the call and then
 * only ever change its `src`.
 *
 * The sequence number is the same idea as the demo page's: whoever bumps it
 * owns the speaker, and an older utterance that wakes up mid-playback finds
 * its number stale and stops. */

const SILENT =
  "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=";

export class Voice {
  private el: HTMLAudioElement | null = null;
  private seq = 0;

  /** Call this synchronously inside a click or tap. Anything later is too late. */
  unlock() {
    if (this.el) return;
    const el = new Audio();
    el.preload = "auto";
    el.src = SILENT;
    void el.play().catch(() => {});
    this.el = el;
  }

  get ready() {
    return this.el !== null;
  }

  /** Play clips in order. Resolves true if it finished, false if interrupted. */
  async say(urls: string[], gapMs = 110): Promise<boolean> {
    if (!this.el || urls.length === 0) return true;
    this.seq += 1;
    const mine = this.seq;
    const el = this.el;

    for (let i = 0; i < urls.length; i += 1) {
      if (mine !== this.seq) return false;

      // Warm the next clip while this one plays. Bulbul returns a whole
      // base64 document with nothing to stream, so a clip not already in
      // cache is a full synthesis round-trip of dead air at the full stop.
      if (i + 1 < urls.length) void fetch(urls[i + 1]).catch(() => {});

      el.src = urls[i];
      const finished = await new Promise<boolean>((done) => {
        // `pause` as well as `ended`: stopping does not fire `ended`, and a
        // wait that never settles hangs the whole call.
        const settle = () => done(mine === this.seq);
        el.onended = settle;
        el.onerror = settle;
        el.onpause = settle;
        el.play().catch(settle);
      });
      if (!finished || mine !== this.seq) return false;

      if (i < urls.length - 1) {
        await new Promise((r) => setTimeout(r, gapMs));
      }
    }
    return mine === this.seq;
  }

  stop() {
    this.seq += 1;
    if (this.el) {
      try {
        this.el.pause();
      } catch {
        /* a paused element that was never played throws on some browsers */
      }
    }
  }
}

/** Does this browser have a microphone we are allowed to ask for?
 *
 *  Answered without prompting: `getUserMedia` is absent entirely on
 *  non-secure origins and on some in-app browsers, and asking for permission
 *  just to find out is the fastest way to get a visitor to say no. */
export function micPossible(): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function" &&
    (window.isSecureContext ?? false)
  );
}
