"""Live viewer for an eval sweep: one clickable URL, any browser tier.

The TypeScript server's ``/session/:id/view`` route only knows Tier-1 sessions
it holds in its own map. Tier-3 sessions are driven from Python against headful
Chrome under Xvfb and never appear there, so half of a sweep is invisible
through that route -- and the port changes every sweep because the harness picks
a free one.

Both tiers already write every frame into the run's ``screenshots/`` directory
with an ``index.jsonl`` carrying the URL, the session id and the step. This
serves those frames instead: a stable URL that follows whichever run is active,
tier-agnostic, no VNC and no extra dependencies.

    python -m eval.viewer                       # http://127.0.0.1:8700
    python -m eval.viewer --port 8700 --experiment ablate10

Read-only: it never touches a run, so it is safe to start and stop mid-sweep.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from eval._bootstrap import REPO_ROOT

RUNS_ROOT = REPO_ROOT / "eval" / "runs"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def _newest_frame(runs_root: Path, experiment: str | None) -> tuple[Path, dict[str, Any]] | None:
    """The most recently written frame across runs, with its index row."""
    pattern = f"{experiment}/*/*/seed*/screenshots" if experiment else "*/*/*/seed*/screenshots"
    best: tuple[float, Path] | None = None
    for shots in runs_root.glob(pattern):
        for p in shots.iterdir():
            if p.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if best is None or mt > best[0]:
                best = (mt, p)
    if best is None:
        return None
    frame = best[1]
    meta: dict[str, Any] = {}
    idx = frame.parent / "index.jsonl"
    if idx.exists():
        try:
            for line in idx.read_text().splitlines():
                row = json.loads(line)
                if row.get("file") == frame.name:
                    meta = row
                    break
        except Exception:
            pass
    return frame, meta


def _rich_viewer_url(session_id: str, tier: str, run_dir: str | None) -> str | None:
    """The real overlay viewer for this session: screencast, cursor, bboxes.

    Tier-1 sessions live in the TypeScript server and are served from whichever
    port that run used (recorded in the run's spec). Tier-3 sessions are served
    by the Python viewer on its own port. Neither is reachable from a single
    fixed address, which is why this page resolves it per run.
    """
    if not session_id:
        return None
    if tier == "t3":
        port = os.environ.get("SUPERBROWSER_T3_VIEWER_PORT", "3101")
        return f"http://127.0.0.1:{port}/t3/session/{session_id}/view"
    base = None
    if run_dir:
        try:
            base = json.loads((Path(run_dir) / "spec.json").read_text()).get("server_url")
        except Exception:
            base = None
    base = base or os.environ.get("SUPERBROWSER_URL") or "http://localhost:3100"
    return f"{base.rstrip('/')}/session/{session_id}/view"


def _run_status(frame: Path) -> dict[str, Any]:
    """Describe the run that produced ``frame`` from its own spec/meta."""
    run_dir = frame.parent.parent
    out: dict[str, Any] = {"run_dir": str(run_dir)}
    try:
        spec = json.loads((run_dir / "spec.json").read_text())
        out.update(run_id=spec.get("run_id"), arm=(spec.get("arm") or {}).get("name"),
                   task_id=(spec.get("task") or {}).get("task_id"),
                   instruction=(spec.get("task") or {}).get("instruction"),
                   model=spec.get("model"), topology=spec.get("topology"))
    except Exception:
        pass
    try:
        experiment = run_dir.parents[2].name
        done = sum(1 for _ in (RUNS_ROOT / experiment).glob("*/*/seed*/run_record.json"))
        out.update(experiment=experiment, runs_finished=done)
    except Exception:
        pass
    out["frames"] = sum(1 for p in frame.parent.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    out["frame"] = frame.name
    out["age_s"] = round(time.time() - frame.stat().st_mtime, 1)
    return out


PAGE = """<!doctype html><meta charset="utf-8"><title>SuperBrowser eval viewer</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0d1117;color:#c9d1d9;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
 header{padding:10px 14px;background:#161b22;border-bottom:1px solid #30363d;
        display:flex;gap:18px;flex-wrap:wrap;align-items:baseline}
 header b{color:#58a6ff;font-weight:600}
 .task{width:100%;color:#8b949e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .wrap{padding:14px;text-align:center}
 img{max-width:100%;border:1px solid #30363d;border-radius:6px}
 .stale{opacity:.45}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;margin-right:6px}
 .dot.idle{background:#8b949e}
 a#rich{color:#58a6ff;text-decoration:none;border:1px solid #1f6feb;border-radius:5px;padding:2px 8px}
 a#rich:hover{background:#1f6feb22}
</style>
<header>
  <span><span class="dot" id="dot"></span><b id="arm">-</b></span>
  <span>run <b id="done">-</b></span>
  <span>frame <b id="frame">-</b> of <b id="frames">-</b></span>
  <span><b id="age">-</b>s ago</span>
  <span>tier <b id="tier">-</b></span>
  <span id="url"></span>
  <a id="rich" href="#" target="_blank" hidden>open full viewer (screencast + bboxes + cursor) &rarr;</a>
  <div class="task" id="task"></div>
</header>
<div class="wrap"><img id="shot" alt="latest frame"></div>
<script>
let last = "";
async function tick(){
  try{
    const T = new URLSearchParams(location.search).get('t');
    const q = (u) => u + (u.includes('?') ? '&' : '?') + '_=' + Date.now() + (T ? '&t=' + T : '');
    const s = await (await fetch(q('/status.json'))).json();
    if(!s.ok){ document.getElementById('arm').textContent = 'waiting for a run…'; return; }
    document.getElementById('arm').textContent   = s.arm || '-';
    document.getElementById('done').textContent  = s.runs_finished ?? '-';
    document.getElementById('frame').textContent = s.frame || '-';
    document.getElementById('frames').textContent= s.frames ?? '-';
    document.getElementById('age').textContent   = s.age_s ?? '-';
    document.getElementById('tier').textContent  = s.tier || '-';
    document.getElementById('task').textContent  = s.instruction || '';
    document.getElementById('url').textContent   = s.url || '';
    const rich = document.getElementById('rich');
    if(s.rich){ rich.href = s.rich; rich.hidden = false; } else { rich.hidden = true; }
    const idle = (s.age_s ?? 99) > 30;
    document.getElementById('dot').className = 'dot' + (idle ? ' idle' : '');
    const img = document.getElementById('shot');
    img.classList.toggle('stale', idle);
    if(s.frame !== last){ last = s.frame; img.src = q('/frame.jpg'); }
  }catch(e){}
}
tick(); setInterval(tick, 1500);
</script>
"""


def make_handler(runs_root: Path, experiment: str | None, token: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def _authorised(self) -> bool:
            """Gate every path behind ?t=<token> when a token is set.

            The page shows whatever the agent is browsing, so binding it to a
            public interface without this would put that on the open internet.
            """
            if not token:
                return True
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(self.path).query)
            if (q.get("t") or [""])[0] == token:
                return True
            hdr = self.headers.get("Authorization", "")
            return hdr.replace("Bearer ", "").strip() == token

        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorised():
                self._send(401, "text/plain", b"add ?t=<token> (printed when the viewer started)")
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
                return
            found = _newest_frame(runs_root, experiment)
            if path == "/frame.jpg":
                if found is None:
                    self._send(404, "text/plain", b"no frames yet")
                    return
                frame = found[0]
                ctype = "image/png" if frame.suffix.lower() == ".png" else "image/jpeg"
                try:
                    self._send(200, ctype, frame.read_bytes())
                except OSError:
                    self._send(503, "text/plain", b"frame is being written")
                return
            if path == "/status.json":
                if found is None:
                    self._send(200, "application/json", json.dumps({"ok": False}).encode())
                    return
                frame, meta = found
                st = _run_status(frame)
                sid = str(meta.get("session_id") or "")
                tier = "t3" if sid.startswith("t3-") else ("t1" if sid else "-")
                st.update(ok=True, url=meta.get("url"), session_id=sid, tier=tier,
                          step=meta.get("step"), source=meta.get("source"),
                          rich=_rich_viewer_url(sid, tier, st.get("run_dir")))
                if st.get("instruction"):
                    st["instruction"] = html.unescape(str(st["instruction"]))[:160]
                self._send(200, "application/json", json.dumps(st).encode())
                return
            self._send(404, "text/plain", b"not found")

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int, runs_root: Path, experiment: str | None, host: str = "127.0.0.1",
          token: str | None = None) -> _Server:
    """Bind the viewer. Defaults to loopback: the page exposes screenshots of
    whatever the agent is browsing, so on a public host it must not be open by
    default. Reach it from a laptop with an SSH tunnel:

        ssh -L 8700:127.0.0.1:8700 <user>@<host>

    Pass host="0.0.0.0" only on a machine where that is acceptable.
    """
    srv = _Server((host, port), make_handler(runs_root, experiment, token))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live viewer for an eval sweep (any browser tier)")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--runs", default=str(RUNS_ROOT))
    ap.add_argument("--experiment", default=None, help="follow only this experiment")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; loopback by default because the page shows the agent's "
                         "screen. Use an SSH tunnel for remote access, or 0.0.0.0 deliberately.")
    ap.add_argument("--token", default=None,
                    help="require ?t=<token>; auto-generated when --host is not loopback")
    args = ap.parse_args(argv)
    token = args.token
    if token is None and args.host not in ("127.0.0.1", "localhost"):
        import secrets

        token = secrets.token_urlsafe(12)
    srv = serve(args.port, Path(args.runs), args.experiment, host=args.host, token=token)
    shown = f"http://{args.host}:{args.port}" + (f"/?t={token}" if token else "")
    print(f"eval viewer: {shown}    (Ctrl-C to stop)")
    print("  follows whichever run is writing frames; works for Tier-1 and Tier-3 alike")
    if args.host == "127.0.0.1":
        print(f"  remote? tunnel it:  ssh -L {args.port}:127.0.0.1:{args.port} <user>@<this-host>")
    else:
        print(f"  bound to {args.host}; the URL above carries a token — anyone with it sees the agent's screen")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
