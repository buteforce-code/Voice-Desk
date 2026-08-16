# Voice Desk — reproducible image.
#
# The reason this exists is not scaling. It is G5.
#
# An eval baseline committed against an unpinned environment is not a baseline:
# a transitive dependency moves, the numbers move, and a regression becomes
# indistinguishable from a rebuild. The suite must run on the same image in CI
# and in production or its output means nothing.
#
# Two consequences fall out for free:
#   * Portability — Railway has no India region. If the residency posture ever
#     has to change, a Dockerfile makes the move to any Indian host a config
#     change rather than a rewrite.
#   * ML deps — Smart Turn v3 ships as ONNX. Auto-detecting builders handle
#     native ML dependencies unevenly; an explicit image does not guess.

# ---------- builder ----------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer first so source edits do not invalidate it.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev 2>/dev/null \
 || uv sync --no-install-project --no-dev

COPY src/ ./src/
RUN uv sync --no-dev


# ---------- runtime ----------
FROM python:3.12-slim-bookworm AS runtime

# libgomp1: onnxruntime (Smart Turn v3, Silero VAD).
# ffmpeg: telephony-band audio resampling, 8kHz <-> 16kHz.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

# Never run the agent as root. It holds telephony and database credentials.
RUN useradd --create-home --uid 10001 voicedesk

WORKDIR /app

COPY --from=builder --chown=voicedesk:voicedesk /app/.venv /app/.venv
COPY --from=builder --chown=voicedesk:voicedesk /app/src /app/src
COPY --chown=voicedesk:voicedesk db/ /app/db/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

USER voicedesk

EXPOSE 8080

# Railway polls this until it returns 200 before routing traffic. For a voice
# service that matters more than usual: a half-warm instance answering a real
# call is a dead call.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["python", "-m", "voicedesk.server"]
