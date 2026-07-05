# All-in-one SuperBrowser image: the TypeScript stealth browser engine + the
# Python orchestrator, exposed as a local RunAgent agent server. Mirrors the
# serverless VM (engine on 127.0.0.1:3100 + in-process orchestrator served via
# `runagent serve`). See docs/sdk.md "Local agent server (Docker)".
#
#   docker compose up        # -> agent server on :8450
#   SuperBrowser(remote=False, local_agent_url="http://localhost:8450").run(...)

# ---------- Stage 1: build the TypeScript engine ----------
FROM node:20-slim AS ts-build
# Don't let puppeteer's postinstall pull its own Chromium — we use the system one.
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
WORKDIR /app
COPY package*.json ./
RUN npm ci                                  # incl. dev deps — tsc is needed to build
COPY tsconfig.json ./
COPY src/ ./src/
COPY bin/ ./bin/
RUN npm run build && npm prune --omit=dev   # compile to build/, then drop dev deps

# ---------- Stage 2: runtime ----------
FROM node:20-slim

# System deps: Chromium (TS puppeteer engine) + extra libs Playwright/patchright
# Chromium needs (libasound2, libxshmfence1, libxkbcommon0) + Python + tini/curl.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation fonts-noto-cjk \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libgtk-3-0 libnspr4 libnss3 libxcomposite1 libxdamage1 libxrandr2 \
    libasound2 libxshmfence1 libxkbcommon0 xdg-utils \
    python3 python3-venv python3-pip \
    curl tini ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Real Google Chrome + Xvfb, so the browser matches the local `npm run dev`
# stealth stack: real Chrome has a cleaner fingerprint than bundled Chromium,
# and Tier-3 runs headful-under-Xvfb (far less detectable than headless). The
# .deb pulls its own deps from the repo lists (still present in this layer).
RUN apt-get update && apt-get install -y --no-install-recommends xvfb wget \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb \
 && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
 && rm -rf /var/lib/apt/lists/*

# Browser identity, matched to the local .env stealth stack:
#   T1 (TS puppeteer engine) -> PUPPETEER_EXECUTABLE_PATH (real Chrome)
#   T3 (python patchright)   -> CHROME_PATH + T3_HEADLESS=0 + auto-Xvfb (headful)
# T3 per-domain profiles persist under ~/.superbrowser/profiles (sb-superbrowser volume).
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable \
    CHROME_PATH=/usr/bin/google-chrome-stable \
    T3_HEADLESS=0 \
    T3_AUTO_XVFB=1 \
    T3_XVFB_DISPLAY=:99 \
    T3_PERSIST_PROFILE=1 \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    SUPERBROWSER_URL=http://127.0.0.1:3100 \
    PORT=3100 \
    HEADLESS=true \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/app \
    RUNAGENT_CACHE_DIR=/home/app/.runagent \
    PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright \
    GIT_PYTHON_REFRESH=quiet

WORKDIR /app

# Dedicated venv (Debian's system Python is PEP 668 "externally managed").
# Putting /opt/venv on PATH also makes `runagent serve`'s sys.executable correct.
RUN python3 -m venv /opt/venv && pip install --upgrade pip

# Bring the built TS engine over (build/ + prod node_modules + bin).
COPY --from=ts-build /app/build/ ./build/
COPY --from=ts-build /app/node_modules/ ./node_modules/
COPY --from=ts-build /app/bin/ ./bin/
COPY package.json ./

# Install the Python package + the runagent SDK. Copy only what pip needs first
# so this layer caches independently of TS source churn. The three workspace
# SOUL.md prompts ride along via nanobot/ (see .dockerignore).
#
# NOTE on the two-step install: runagent hard-pins `websockets==15.0.1` while
# nanobot-ai requires `websockets>=16.0` — an unsolvable pin clash for pip, even
# though runagent works fine on websockets 16 (stable `connect` API). So we (1)
# install our package + runagent's OTHER deps via the resolver — starlette
# co-resolves to 0.41 (mcp accepts >=0.27, runagent's fastapi 0.115 wants <0.42),
# websockets to 16.x — then (2) install runagent itself with --no-deps so its
# websockets pin can't veto nanobot-ai. The explicit deps mirror runagent's set.
COPY pyproject.toml README.md LICENSE ./
COPY nanobot/ ./nanobot/
RUN pip install "." \
      "fastapi==0.115.12" "uvicorn==0.34.1" "sqlalchemy==2.0.41" \
      "jsonpath-ng==1.7.0" "GitPython>=3.1.43" "inquirer>=3.4.0" \
 && pip install --no-deps "runagent>=0.1.40" \
 && python -c "import runagent, fastapi, sqlalchemy, jsonpath_ng, git, inquirer; from runagent import RunAgentClient; print('runagent import OK')"

# The RunAgent agent manifest the local server serves (main.py:run + config).
COPY deploy/ ./deploy/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

# Non-root user owns /app + its HOME (browser cache, runagent DB, persistent dirs).
# UID 1001 — the node:20-slim base already uses 1000 for its `node` user.
RUN useradd -m -u 1001 -s /bin/bash app \
    && mkdir -p /home/app/.runagent /home/app/.superbrowser /home/app/.nanobot \
               /home/app/.cache/ms-playwright /tmp/superbrowser/downloads \
    && chown -R app:app /app /home/app /tmp/superbrowser

USER app

# Patchright's stealth Chromium for the Python Tier-3 fetch. Installed AS app so
# it lands under $PLAYWRIGHT_BROWSERS_PATH (/home/app/...) and is readable at run.
RUN patchright install chromium

EXPOSE 8450
# tini as PID 1: reaps the backgrounded node engine and forwards signals.
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
