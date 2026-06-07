#!/usr/bin/env bash
#
# SuperBrowser one-command installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/runagent-dev/runagent-superbrowser/main/scripts/install.sh | bash
#
# Or, from a checkout:   bash scripts/install.sh
#
# Auto-installs the project-specific bits (Google Chrome, Xvfb + system libs on
# Linux, the patchright Chromium, Python venv + deps, the TS build, and a .env).
# For the Node / Python *runtimes* it detects and instructs rather than silently
# changing them — so it won't fight nvm / pyenv / asdf.
#
# Flags:
#   -y, --yes     non-interactive (assume yes; needed for the curl | bash path)
#       --check   dry-run: print what would happen, change nothing
#       --dir D   install/clone into D (default: ./runagent-superbrowser, or cwd if already a checkout)
#   -h, --help    this help
set -euo pipefail

REPO_URL="https://github.com/runagent-dev/runagent-superbrowser.git"
ASSUME_YES=0
DRY_RUN=0
TARGET_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --check)  DRY_RUN=1 ;;
    --dir)    TARGET_DIR="${2:-}"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---- pretty output ---------------------------------------------------------
c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[1m'; c_0=$'\033[0m'
say()  { printf "${c_g}==>${c_0} %s\n" "$*"; }
warn() { printf "${c_y}!  ${c_0} %s\n" "$*"; }
err()  { printf "${c_r}✗  ${c_0} %s\n" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf "   ${c_b}[dry-run]${c_0} %s\n" "$*"; else eval "$@"; fi; }

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  [ "$DRY_RUN" = 1 ] && return 0
  printf "${c_y}?  ${c_0}%s [Y/n] " "$1"; read -r reply </dev/tty || reply="y"
  case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# ---- platform detection ----------------------------------------------------
OS="$(uname -s)"
PKG=""          # apt | dnf | brew
SUDO=""
case "$OS" in
  Darwin) PKG="brew" ;;
  Linux)
    if have apt-get; then PKG="apt"
    elif have dnf;   then PKG="dnf"
    elif have yum;   then PKG="yum"
    else warn "no apt/dnf/yum found — you'll need to install system packages by hand"; fi
    if [ "$(id -u)" != 0 ]; then SUDO="sudo"; fi
    ;;
  *) err "unsupported OS '$OS' — use scripts/install.ps1 on Windows"; exit 1 ;;
esac
say "Platform: ${c_b}$OS${c_0} (package manager: ${PKG:-none})"

# ---- 1. runtimes: detect & instruct (do NOT auto-install) ------------------
NODE_OK=0; PY=""
if have node; then
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  [ "${major:-0}" -ge 20 ] && NODE_OK=1
fi
for cand in python3.12 python3.11 python3; do
  if have "$cand"; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then PY="$cand"; break; fi
  fi
done

if [ "$NODE_OK" = 0 ] || [ -z "$PY" ]; then
  err "Missing runtime(s). Install these, then re-run this script:"
  if [ "$NODE_OK" = 0 ]; then
    case "$PKG" in
      brew) echo "    brew install node" ;;
      apt)  echo "    curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO bash - && $SUDO apt-get install -y nodejs" ;;
      dnf|yum) echo "    $SUDO $PKG module install -y nodejs:20  # or use nvm" ;;
      *)    echo "    install Node.js 20+ from https://nodejs.org" ;;
    esac
  fi
  if [ -z "$PY" ]; then
    case "$PKG" in
      brew) echo "    brew install python@3.11" ;;
      apt)  echo "    $SUDO apt-get install -y python3.11 python3.11-venv python3-pip" ;;
      dnf|yum) echo "    $SUDO $PKG install -y python3.11" ;;
      *)    echo "    install Python 3.11+ from https://python.org" ;;
    esac
  fi
  exit 1
fi
say "Node $(node -v) and $($PY --version) detected"

# ---- 2. locate / clone the repo -------------------------------------------
in_checkout() { [ -f package.json ] && grep -q '"runagent-superbrowser"' package.json 2>/dev/null; }
if [ -n "$TARGET_DIR" ]; then
  if [ ! -d "$TARGET_DIR/.git" ]; then run "git clone '$REPO_URL' '$TARGET_DIR'"; fi
  run "cd '$TARGET_DIR'"; [ "$DRY_RUN" = 0 ] && cd "$TARGET_DIR"
elif ! in_checkout; then
  say "Cloning $REPO_URL"
  run "git clone '$REPO_URL' runagent-superbrowser"
  run "cd runagent-superbrowser"; [ "$DRY_RUN" = 0 ] && cd runagent-superbrowser
else
  say "Using existing checkout: $(pwd)"
fi

# ---- 3. Google Chrome (auto) ----------------------------------------------
chrome_present() {
  for p in /usr/bin/google-chrome-stable /usr/bin/google-chrome \
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    [ -x "$p" ] && return 0
  done
  have google-chrome || have google-chrome-stable
}
if chrome_present; then
  say "Google Chrome already installed"
elif confirm "Install Google Chrome (recommended — fingerprint targets need real Chrome)?"; then
  case "$PKG" in
    brew) run "brew install --cask google-chrome" ;;
    apt)
      run "wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | $SUDO gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg"
      run "echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' | $SUDO tee /etc/apt/sources.list.d/google-chrome.list >/dev/null"
      run "$SUDO apt-get update -qq && $SUDO apt-get install -y google-chrome-stable" ;;
    dnf|yum)
      run "$SUDO $PKG install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm" ;;
    *) warn "install Chrome manually from https://google.com/chrome" ;;
  esac
fi

# ---- 4. Linux system libs + Xvfb (auto) -----------------------------------
if [ "$OS" = "Linux" ] && [ "$PKG" = "apt" ]; then
  LIBS="xvfb fonts-liberation fonts-noto-cjk libatk-bridge2.0-0 libatk1.0-0 libcups2 \
libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libxcomposite1 libxdamage1 libxrandr2 xdg-utils"
  if confirm "Install Xvfb + headless-Chrome system libraries (apt)?"; then
    run "$SUDO apt-get update -qq && $SUDO apt-get install -y $LIBS"
  fi
elif [ "$OS" = "Linux" ]; then
  warn "non-apt Linux — install Xvfb and Chrome's shared libs via your package manager if headful Tier-3 is needed"
fi

# ---- 5. Python venv + deps + patchright browser ---------------------------
say "Setting up Python venv + dependencies"
run "$PY -m venv venv"
VENV_PY="venv/bin/python"
run "$VENV_PY -m pip install --upgrade pip -q"
run "$VENV_PY -m pip install -r requirements.txt"
run "venv/bin/patchright install chromium"
if [ "$OS" = "Linux" ]; then run "venv/bin/playwright install-deps chromium || true"; fi

# ---- 6. Node deps + build --------------------------------------------------
say "Installing Node dependencies + building the engine"
if [ -f package-lock.json ]; then run "npm ci"; else run "npm install"; fi
run "npm run build"

# ---- 7. .env ---------------------------------------------------------------
if [ ! -f .env ] && [ -f .env.example ]; then
  run "cp .env.example .env"
  say "Created .env from .env.example — edit the keys you care about"
fi

# ---- 8. doctors ------------------------------------------------------------
if [ "$DRY_RUN" = 0 ]; then
  say "Verifying install"
  node bin/superbrowser-doctor.js || true
  echo
  PYTHONPATH=nanobot "$VENV_PY" -m superbrowser_bridge.doctor || true
fi

echo
say "${c_b}Done.${c_0} Next:"
echo "    1. (optional) edit .env and run: venv/bin/python -m nanobot onboard   # set your LLM keys"
echo "    2. start the engine:   npm start"
echo "    3. run a task:         venv/bin/python nanobot/run.py \"find trending Python repos on GitHub\""
