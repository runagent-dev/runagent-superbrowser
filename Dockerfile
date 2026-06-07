FROM node:20-slim

# Install Chromium and dependencies
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
    fonts-noto-cjk \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium path
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

WORKDIR /app

# Install Node.js dependencies (incl. dev — tsc is needed to build)
COPY package*.json ./
RUN npm ci

# Copy source and build, then drop dev deps to keep the image lean
COPY tsconfig.json ./
COPY src/ ./src/
COPY bin/ ./bin/
RUN npm run build && npm prune --omit=dev

# Install nanobot (optional, for MCP integration)
COPY nanobot/ ./nanobot/

# Create download directory
RUN mkdir -p /tmp/superbrowser/downloads

EXPOSE 3100

CMD ["node", "build/index.js"]
