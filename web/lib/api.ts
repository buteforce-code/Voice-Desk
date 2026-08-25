/* The browser's view of the Railway backend.
 *
 * Everything a caller says goes straight from their browser to the Python
 * service and nowhere else. Vercel serves static HTML and never sees a word
 * of it -- which is not an optimisation, it is the residency rule: caller
 * data lands in one place, in India, and a marketing host is not that place. */

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/$/, "");

export type Snapshot = {
  reply: string;
  clips: string[];
  state: string;
  states: string[];
  terminal: boolean;
  identity_verified: boolean;
  tools: { name: string; code: string }[];
  appointments: { doctor: string; specialty: string; starts_at: string }[];
  blocked: boolean;
  categories: string[];
  language?: string;
  call_id?: string;
};

export type Config = {
  clinic: string;
  model: string;
  specialties: string[];
  hold_lines: Record<string, string[]>;
  fill_after_ms: number;
  live_stt: boolean;
};

export class BackendDown extends Error {
  constructor(cause?: unknown) {
    super("backend unreachable");
    this.name = "BackendDown";
    this.cause = cause;
  }
}

/** Every call is bounded. A cold Railway container can take a while to wake,
 *  and a fetch with no timeout is how a visitor gets a spinner that never
 *  resolves and decides the product is broken. */
async function call<T>(path: string, init: RequestInit = {}, ms = 45_000): Promise<T> {
  if (!API_BASE) throw new BackendDown("NEXT_PUBLIC_API_BASE is not set");
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), ms);
  try {
    const res = await fetch(API_BASE + path, {
      ...init,
      signal: abort.signal,
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((body as { error?: string }).error ?? `HTTP ${res.status}`);
    return body as T;
  } catch (err) {
    if (err instanceof Error && (err.name === "AbortError" || err.name === "TypeError")) {
      throw new BackendDown(err);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export const getConfig = () => call<Config>("/api/config", { method: "GET" }, 12_000);

export const startCall = (language: string | null) =>
  call<Snapshot>("/api/start", { method: "POST", body: JSON.stringify({ language }) });

export const sendTurn = (callId: string, text: string, language: string | null) =>
  call<Snapshot>("/api/turn", {
    method: "POST",
    body: JSON.stringify({ call_id: callId, text, language }),
  });

export const ttsUrl = (text: string, language: string) =>
  `${API_BASE}/api/tts?${new URLSearchParams({ text, language })}`;

/** Is the backend awake? Used once, on mount, to decide whether the hero
 *  offers a live call or quietly runs the recorded one instead. Never throws:
 *  the answer is a boolean, and "no" is a normal answer. */
export async function isAwake(): Promise<boolean> {
  if (!API_BASE) return false;
  try {
    await getConfig();
    return true;
  } catch {
    return false;
  }
}
