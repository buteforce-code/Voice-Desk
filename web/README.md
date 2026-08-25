# Voice Desk — the product page

A static Next.js site on Vercel, talking to the Python demo backend on Railway.

## Why the split

Vercel serves HTML from a CDN. Every word a caller types or says goes from
their browser **straight to the Railway container** and nowhere else — the site
host never sees it. That is not an optimisation; it is CLAUDE.md's residency
rule made structural. `output: "export"` in `next.config.ts` is what keeps it
honest: there is no Next.js server, so there is nowhere for caller data to
accidentally land.

## Running it locally

```bash
# 1. the backend, from the repo root
DEMO_ALLOWED_ORIGINS=http://localhost:3000 python -m voicedesk.demo.server

# 2. the site
cd web && npm install && npm run dev
```

Then open **http://localhost:3000** — not `127.0.0.1`. Next 16's dev server
rejects cross-origin requests for its own chunks, and the two hostnames are
different origins, so `127.0.0.1:3000` serves the HTML and 403s every script.

## Deploying

### Backend → Railway

**Live:** https://demo-production-5766.up.railway.app

The service is attached to **`railway.demo.toml`** via its `railwayConfigFile` setting, and that file -- not the dashboard, not the API -- is what selects `Dockerfile.demo`. A config file in the repo beats both.

Point a service at the repo with **`Dockerfile.demo`** as the build source.
(`Dockerfile` is the real pipeline service — a different, heavier image the
G5 eval baseline is pinned against. The two must not deploy together.)

Variables:

| Variable | Value |
|---|---|
| `DEMO_ALLOWED_ORIGINS` | `https://your-site.vercel.app` — comma-separated, **no wildcard** |
| `SARVAM_API_KEY` | speech, both directions |
| `OPENROUTER_API_KEY` *or* `GOOGLE_AI_API_KEY` | reasoning |
| `LLM_PROVIDER` | `openrouter` or `google` |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` — standing org-wide block |
| `DEMO_CALLS_PER_HOUR` | optional, default 12 |
| `DEMO_TURNS_PER_HOUR` | optional, default 90 |

`PORT` is injected by Railway and the server binds it. Health check is
`/health`.

### Frontend -> Vercel

**Live:** https://voice-desk-admin-11219769s-projects.vercel.app

Project `voice-desk`, root directory `web`. Two settings in `vercel.json` fail
in opposite directions and both are needed:

- `framework: "nextjs"` -- the project's own framework setting is `null` (the
  new-project dialog auto-detects **Python**, because that is what the repo
  root is). Without this Vercel takes the plain-static path and looks for a
  `public/` directory.
- **no** `outputDirectory` -- Vercel already understands `output: "export"`.
  Setting it to `out` makes the Next builder hunt for `routes-manifest.json`
  among the exported files.


Root directory `web`. Set:

```
NEXT_PUBLIC_API_BASE = https://demo-production-5766.up.railway.app
```

It is `NEXT_PUBLIC_`, so it is baked into the bundle and visible to anyone —
which is fine, it is a public URL. The **backend's** allowlist is what stops
other sites spending your credits, so set `DEMO_ALLOWED_ORIGINS` before you
share the link.

## What happens when the backend is down

The hero notices on mount and plays a recorded call instead, labelled as a
recording. A visitor never sees a connection error, and never sees a recording
pretending to be live. Every transcript in `lib/replay.ts` is real captured
output, not landing-page copy.

## Known gaps

- **Voice input is not wired into the site yet.** Typing works everywhere; the
  microphone path exists in the demo page at `src/voicedesk/demo/index.html`
  and still needs porting here, along with the WebSocket bridge — which is a
  second port and therefore a second Railway service.
- The Tamil and Hindi replays show the agent transliterating specialty and
  doctor names (`ஜெனரல் மெடிசின்`, `डॉक्टर अनिता वरदान`) where `prompts.py`
  says never to. Real output, real bug, not yet fixed.
