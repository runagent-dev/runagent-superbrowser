<#
.SYNOPSIS
  SuperBrowser one-command installer for Windows.

.DESCRIPTION
  Auto-installs the project-specific bits (Google Chrome via winget/choco, the
  patchright Chromium, a Python venv + deps, the TS build, and a .env). Detects
  and instructs for the Node / Python runtimes rather than changing them, so it
  won't fight your version manager. Windows runs headful by default (no Xvfb /
  apt needed), so HEADLESS=false is the right setting in .env.

  Run it:
    irm https://raw.githubusercontent.com/runagent-dev/runagent-superbrowser/main/scripts/install.ps1 | iex
  Or, from a checkout:
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

.PARAMETER Yes    Non-interactive (assume yes).
.PARAMETER Check  Dry-run: print what would happen, change nothing.
.PARAMETER Dir    Install/clone into this directory.
#>
param(
  [switch]$Yes,
  [switch]$Check,
  [string]$Dir = ""
)
$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/runagent-dev/runagent-superbrowser.git"

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!   $m" -ForegroundColor Yellow }
function Err  ($m) { Write-Host "x   $m" -ForegroundColor Red }
function Have ($c) { return [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Run  ($cmd) {
  if ($Check) { Write-Host "   [dry-run] $cmd" -ForegroundColor Cyan }
  else { Invoke-Expression $cmd }
}
function Confirm ($m) {
  if ($Yes -or $Check) { return $true }
  $r = Read-Host "?   $m [Y/n]"
  return ($r -notmatch '^[nN]')
}

Say "Platform: Windows"

# ---- 1. runtimes: detect & instruct ---------------------------------------
$NodeOk = $false
if (Have node) {
  $major = (node -p "process.versions.node.split('.')[0]") 2>$null
  if ([int]$major -ge 20) { $NodeOk = $true }
}
$Py = $null
foreach ($cand in @("python", "py")) {
  if (Have $cand) {
    try {
      $v = (& $cand -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null)
      if ($v -and [version]$v -ge [version]"3.11") { $Py = $cand; break }
    } catch { }
  }
}

if (-not $NodeOk -or -not $Py) {
  Err "Missing runtime(s). Install these, then re-run:"
  if (-not $NodeOk) { Write-Host "    winget install OpenJS.NodeJS.LTS    # or from https://nodejs.org" }
  if (-not $Py)     { Write-Host "    winget install Python.Python.3.11   # or from https://python.org" }
  exit 1
}
Say "Node $(node -v) and Python ($Py) detected"

# ---- 2. locate / clone the repo -------------------------------------------
function In-Checkout { (Test-Path package.json) -and (Select-String -Quiet -Path package.json -Pattern '"runagent-superbrowser"') }
if ($Dir) {
  if (-not (Test-Path "$Dir/.git")) { Run "git clone $RepoUrl `"$Dir`"" }
  if (-not $Check) { Set-Location $Dir }
} elseif (-not (In-Checkout)) {
  Say "Cloning $RepoUrl"
  Run "git clone $RepoUrl runagent-superbrowser"
  if (-not $Check) { Set-Location runagent-superbrowser }
} else {
  Say "Using existing checkout: $(Get-Location)"
}

# ---- 3. Google Chrome (auto via winget, choco fallback) --------------------
$chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
if (Test-Path $chrome) {
  Say "Google Chrome already installed"
} elseif (Confirm "Install Google Chrome (recommended)?") {
  if (Have winget)    { Run "winget install --id Google.Chrome -e --source winget --accept-source-agreements --accept-package-agreements" }
  elseif (Have choco) { Run "choco install -y googlechrome" }
  else { Warn "install Chrome manually from https://google.com/chrome" }
}

# ---- 4. Python venv + deps + patchright browser ---------------------------
Say "Setting up Python venv + dependencies"
Run "$Py -m venv venv"
$VenvPy = "venv\Scripts\python.exe"
Run "$VenvPy -m pip install --upgrade pip -q"
Run "$VenvPy -m pip install -r requirements.txt"
Run "venv\Scripts\patchright.exe install chromium"
# (no `playwright install-deps` on Windows — that's a Linux/apt shim)

# ---- 5. Node deps + build --------------------------------------------------
Say "Installing Node dependencies + building the engine"
if (Test-Path package-lock.json) { Run "npm ci" } else { Run "npm install" }
Run "npm run build"

# ---- 6. .env (headful default for desktop) --------------------------------
if (-not (Test-Path .env) -and (Test-Path .env.example)) {
  Run "Copy-Item .env.example .env"
  if (-not $Check) {
    (Get-Content .env) -replace '^HEADLESS=.*', 'HEADLESS=false' | Set-Content .env
  }
  Say "Created .env (HEADLESS=false for a desktop) — edit the keys you care about"
}

# ---- 7. doctors ------------------------------------------------------------
if (-not $Check) {
  Say "Verifying install"
  node bin\superbrowser-doctor.js
  $env:PYTHONPATH = "nanobot"
  & $VenvPy -m superbrowser_bridge.doctor
}

Write-Host ""
Say "Done. Next:"
Write-Host "    1. (optional) venv\Scripts\python.exe -m nanobot onboard   # set your LLM keys"
Write-Host "    2. start the engine:   npm start"
Write-Host "    3. run a task:         venv\Scripts\python.exe nanobot\run.py ""find trending Python repos on GitHub"""
