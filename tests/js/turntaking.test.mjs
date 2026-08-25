/* Runs the REAL page script from index.html under stubs.

   Nothing here re-implements the turn-taking logic -- the source is extracted
   from the page, given a DOM/Audio/WebSocket shim, and exercised. A test that
   copied the logic would pass while the page was broken. */

import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert";

const PAGE = process.argv[2];
const raw = readFileSync(PAGE, "utf8");
const body = raw.split('<script type="module">')[1].split("</script>")[0];

// Strip the CDN import; the shim provides `animate`/`stagger`.
const src = body.replace(/^import .*$/m, "");

/* -- stubs ------------------------------------------------------------- */

let now = 1_000_000;
const clock = { advance: (ms) => (now += ms) };

class FakeAudio {
  static played = [];
  static duration = 40;
  constructor(src) { this.src = src; this.paused = false; FakeAudio.made.push(this); }
  play() {
    if (this.paused) return Promise.resolve();
    FakeAudio.played.push(this.src);
    this._t = setTimeout(() => { if (!this.paused && this.onended) this.onended(); },
                         FakeAudio.duration);
    return Promise.resolve();
  }
  // Browsers fire `pause` here and do NOT fire `ended`. The page must not
  // depend on `ended` to unblock, so the stub must not be kinder than reality.
  pause() {
    const wasPlaying = !this.paused;
    this.paused = true; clearTimeout(this._t);
    if (wasPlaying && this.onpause) this.onpause();
  }
}
FakeAudio.made = [];

const el = () => {
  const node = {
    textContent: "", innerHTML: "", className: "", value: "",
    children: [], style: {}, scrollTop: 0, scrollHeight: 0,
    classList: { add() {}, remove() {} },
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, remove() {}, addEventListener() {},
    querySelector: () => el(), firstElementChild: null,
  };
  return node;
};

const sandbox = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, Math, JSON, Date: class extends Date { static now() { return now; } },
  URLSearchParams, Set, Map, Array, Object, String, Number, Boolean, Error,
  animate: () => ({ finished: Promise.resolve() }),
  stagger: () => 0,
  Audio: FakeAudio,
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  WebSocket: class { constructor() { this.readyState = 0; } send() {} close() {} },
  AudioContext: class { close() {} },
  navigator: { mediaDevices: { getUserMedia: () => Promise.reject(new Error("no mic")) } },
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  URL: { createObjectURL: () => "blob:x" },
  Blob: class {},
  document: { getElementById: () => el(), createElement: () => el(), title: "" },
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;

const EXPOSE = `
;globalThis.__t = {
  speak, stopSpeaking, isBargeIn, isEcho, words, onBridge,
  set(k,v){ if(k==="speaking") speaking=v; if(k==="saying") saying=v;
            if(k==="spokeAt") spokeAt=v; if(k==="ended") ended=v;
            if(k==="busy") busy=v; if(k==="language") language=v;
            if(k==="finalText") finalText=v; },
  get(){ return {speaking, speakSeq, saying, spokeAt, finalText, partial}; },
  consts(){ return {GAP_MS, BARGE_GUARD_MS, BARGE_MIN_WORDS, ECHO_OVERLAP}; },
};
`;

vm.createContext(sandbox);
new vm.Script(src + EXPOSE).runInContext(sandbox);
const T = sandbox.__t;

const tick = (ms) => new Promise((r) => { clock.advance(ms); setTimeout(r, ms); });

let failures = 0;
async function test(name, fn) {
  FakeAudio.played = []; FakeAudio.made = [];
  T.stopSpeaking();
  T.set("ended", false); T.set("busy", false); T.set("finalText", "");
  T.set("language", "en-IN");
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failures++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}

/* -- the tests --------------------------------------------------------- */

console.log("\nturn-taking (real page source)\n");

await test("every clip is played, in order", async () => {
  const done = T.speak(["One.", "Two.", "Three."]);
  await tick(600);
  assert.strictEqual(await done, true, "speak() should report it finished");
  assert.strictEqual(FakeAudio.played.length, 3, "all three clips play");
  assert.ok(FakeAudio.played[0].includes("One."), "first clip first");
  assert.ok(FakeAudio.played[2].includes("Three."), "last clip last");
});

await test("clips are requested in parallel, not one after the last ends", async () => {
  const done = T.speak(["A.", "B.", "C."]);
  await tick(1);
  // All three Audio objects exist immediately -- the browser fetches them at
  // once, so the caller waits for clip one to synthesise, not all three.
  assert.strictEqual(FakeAudio.made.length, 3, "all clips requested up front");
  await tick(600); await done;
});

await test("the caller's language rides on every clip request", async () => {
  T.set("language", "ta-IN");
  const done = T.speak(["Vanakkam."]);
  await tick(200); await done;
  assert.ok(FakeAudio.played[0].includes("language=ta-IN"), FakeAudio.played[0]);
});

await test("a real interruption stops playback and reports false", async () => {
  const done = T.speak(["One.", "Two.", "Three.", "Four."]);
  await tick(10);
  T.stopSpeaking();
  assert.strictEqual(await done, false, "speak() must report it was cut off");
  const played = FakeAudio.played.length;
  await tick(600);
  assert.strictEqual(FakeAudio.played.length, played,
    "nothing queued behind the interruption may play afterwards");
});

await test("the desk's own voice is not an interruption", async () => {
  T.set("saying", "Doctor Ragunandan has an opening at nine in the morning.");
  T.set("speaking", true);
  T.set("spokeAt", now - 5000);
  assert.strictEqual(
    T.isBargeIn({ final: true, text: "doctor ragunandan has an opening at nine" }),
    false, "echo of the current utterance must be rejected");
});

await test("a real interruption during that same utterance gets through", async () => {
  T.set("saying", "Doctor Ragunandan has an opening at nine in the morning.");
  T.set("speaking", true);
  T.set("spokeAt", now - 5000);
  assert.strictEqual(
    T.isBargeIn({ final: true, text: "no wait make it Thursday instead" }),
    true, "words that are not ours must interrupt");
});

await test("a backchannel does not stop the sentence it agrees with", async () => {
  T.set("saying", "Shall I book that?"); T.set("spokeAt", now - 5000);
  assert.strictEqual(T.isBargeIn({ final: true, text: "mm" }), false);
  assert.strictEqual(T.isBargeIn({ final: true, text: "yes" }), false,
    "one word is a backchannel, not an interruption");
});

await test("an interim guess never interrupts", async () => {
  T.set("saying", "Shall I book that?"); T.set("spokeAt", now - 5000);
  assert.strictEqual(
    T.isBargeIn({ final: false, text: "actually no I changed my mind" }), false);
});

await test("the tail of the previous utterance does not cut off the reply", async () => {
  T.set("saying", "Shall I book that?");
  T.set("spokeAt", now - 50);          // playback started 50ms ago
  assert.strictEqual(
    T.isBargeIn({ final: true, text: "tomorrow morning please" }), false,
    "inside the guard window this is the caller's own last words, still decoding");
  T.set("spokeAt", now - 5000);
  assert.strictEqual(
    T.isBargeIn({ final: true, text: "tomorrow morning please" }), true);
});

await test("echo detection is case and punctuation insensitive", async () => {
  T.set("saying", "Confirmed, Doctor Rao at nine.");
  assert.strictEqual(T.isEcho("CONFIRMED doctor rao, at nine!!"), true);
  assert.strictEqual(T.isEcho("cancel it please"), false);
});

await test("an empty reply is a no-op rather than a hang", async () => {
  assert.strictEqual(await T.speak([]), true);
  assert.strictEqual(await T.speak(""), true);
  assert.strictEqual(FakeAudio.played.length, 0);
});

await test("a plain string still plays (the pre-clips fallback path)", async () => {
  const done = T.speak("Just the one sentence.");
  await tick(200);
  assert.strictEqual(await done, true);
  assert.strictEqual(FakeAudio.played.length, 1);
});

const c = T.consts();
await test("the tuning constants are sane", async () => {
  assert.ok(c.BARGE_GUARD_MS >= 200 && c.BARGE_GUARD_MS <= 600, "guard window");
  assert.ok(c.BARGE_MIN_WORDS >= 2, "at least two words to interrupt");
  assert.ok(c.ECHO_OVERLAP > 0 && c.ECHO_OVERLAP <= 1, "overlap is a share");
  assert.ok(c.GAP_MS > 0 && c.GAP_MS < 400, "inter-sentence gap");
});

console.log(failures ? `\n${failures} failed\n` : "\nall passed\n");
process.exit(failures ? 1 : 0);
