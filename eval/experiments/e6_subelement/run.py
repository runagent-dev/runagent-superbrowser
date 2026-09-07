"""Run the sub-element targeting suite against the TypeScript browser server.

For every fixture item the runner hands the snapper the rect of the MERGED
row (what vision returns in the paper's "United States ▼" failure case) plus
the vision-style label, dispatches ``POST /session/:id/click`` and reads back
which element actually received the click (``recorder.js``). A hit means the
recorded element is the intended sub-element or inside it. As a sanity
baseline each item is also clicked with the exact rect of the intended
element (condition ``exact``).

Strategies are TypeScript-side (``SUPERBROWSER_SNAP_STRATEGY``), so the runner
restarts the server per strategy (``--manage-server``, default here) or runs
the single strategy the current server was started with
(``--strategies X --assume-server``).

    python -m eval.experiments.e6_subelement.run --manage-server
    python -m eval.experiments.e6_subelement.analyze
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from eval._bootstrap import REPO_ROOT
from eval.core import report, server
from eval.core.server import ServerManager

NAME = "e6_subelement"
FIXTURES = Path(__file__).parent / "fixtures"
STRATEGIES = ("chevron", "center", "dom_alt")
CONDITIONS = ("merged_row", "exact")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:  # silence
        return


def serve_fixtures() -> tuple[socketserver.TCPServer, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    handler = functools.partial(_Quiet, directory=str(FIXTURES))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=60.0)

    def create(self, url: str) -> str:
        r = self.http.post(f"{self.base}/session/create", json={"url": url})
        r.raise_for_status()
        return r.json()["sessionId"]

    def navigate(self, sid: str, url: str) -> None:
        self.http.post(f"{self.base}/session/{sid}/navigate", json={"url": url}).raise_for_status()

    def evaluate(self, sid: str, script: str) -> Any:
        r = self.http.post(f"{self.base}/session/{sid}/evaluate", json={"script": script})
        r.raise_for_status()
        return r.json().get("result")

    def click_bbox(self, sid: str, bbox: dict[str, float], label: str) -> dict[str, Any]:
        r = self.http.post(f"{self.base}/session/{sid}/click", json={"bbox": bbox, "expected_label": label})
        try:
            body = r.json()
        except Exception:
            body = {"error": r.text[:200]}
        body["_status"] = r.status_code
        return body

    def close(self, sid: str) -> None:
        try:
            self.http.delete(f"{self.base}/session/{sid}")
        except Exception:
            pass


def rect_of(client: Client, sid: str, selector: str) -> dict[str, float] | None:
    res = client.evaluate(sid, f"""(() => {{ const el = document.querySelector({json.dumps(selector)});
        if (!el) return null; const r = el.getBoundingClientRect();
        return {{x0: r.left, y0: r.top, x1: r.right, y1: r.bottom}}; }})()""")
    return res if isinstance(res, dict) else None


def last_click(client: Client, sid: str, intended_id: str) -> dict[str, Any]:
    return client.evaluate(sid, f"""(() => {{ const c = window.__clicks || []; const last = c[c.length - 1] || null;
        const intended = document.getElementById({json.dumps(intended_id)});
        let hit = false;
        if (last && intended) {{ const rec = last.id ? document.getElementById(last.id) : null;
            hit = !!(rec && (rec === intended || intended.contains(rec))); }}
        return {{n: c.length, last: last, hit: hit}}; }})()""") or {"n": 0, "last": None, "hit": False}


def run_strategy(strategy: str, client: Client, base_url: str, items: list[dict[str, Any]], *, conditions=CONDITIONS) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_page: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_page.setdefault(it["page"], []).append(it)
    for page, page_items in by_page.items():
        url = f"{base_url}/{page}"
        sid = client.create(url)
        try:
            for it in page_items:
                for cond in conditions:
                    client.navigate(sid, url)
                    time.sleep(0.15)
                    sel = it["row_selector"] if cond == "merged_row" else f"#{it['intended_id']}"
                    bbox = rect_of(client, sid, sel)
                    if not bbox:
                        rows.append({"strategy": strategy, "condition": cond, **{k: it[k] for k in ("id", "family", "control", "expected_label", "intended_id")},
                                     "error": f"no rect for {sel}", "hit": False})
                        continue
                    resp = client.click_bbox(sid, bbox, it["expected_label"])
                    time.sleep(0.15)
                    rec = last_click(client, sid, it["intended_id"])
                    snap = resp.get("snap") or {}
                    rows.append({
                        "strategy": strategy, "condition": cond, "id": it["id"], "family": it["family"], "control": it["control"],
                        "expected_label": it["expected_label"], "intended_id": it["intended_id"], "note": it.get("note"),
                        "bbox": bbox, "status": resp.get("_status"), "error": resp.get("error"),
                        "snapped": snap.get("snapped"), "method": snap.get("method"), "label_score": snap.get("label_score"),
                        "chevron_score": snap.get("chevron_score"), "candidates": snap.get("candidates"), "target": snap.get("target"),
                        "click_x": snap.get("x"), "click_y": snap.get("y"),
                        "recorded_id": (rec.get("last") or {}).get("id"), "recorded_tag": (rec.get("last") or {}).get("tag"),
                        "n_clicks": rec.get("n"), "hit": bool(rec.get("hit")),
                    })
                    print(f"  [{strategy}/{cond}] {it['id']:16s} -> {(rec.get('last') or {}).get('id')!s:20s} hit={rec.get('hit')} method={snap.get('method')}")
        finally:
            client.close(sid)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E6 sub-element targeting suite")
    ap.add_argument("--strategies", default=",".join(STRATEGIES))
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--manage-server", action="store_true", help="restart the TS server per strategy (default when >1 strategy)")
    ap.add_argument("--assume-server", action="store_true", help="the running server already has the single requested strategy")
    ap.add_argument("--items", default="all", help="comma-separated fixture item ids")
    ap.add_argument("--server-port", type=int, default=None, help="port for the harness-managed server (free port by default)")
    args = ap.parse_args(argv)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    conditions = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    items = manifest["items"]
    if args.items != "all":
        want = {x.strip() for x in args.items.split(",")}
        items = [it for it in items if it["id"] in want]
    manage = args.manage_server or (len(strategies) > 1 and not args.assume_server)
    out = report.out_dir(NAME)
    httpd, base_url = serve_fixtures()
    log_dir = REPO_ROOT / "eval" / "runs" / NAME / "_logs"
    servers = ServerManager(manage=manage, log_dir=log_dir, port=args.server_port,
                            assumed_env=None if manage else ({"SUPERBROWSER_ALLOW_LOCAL_FIXTURES": "1", **({"SUPERBROWSER_SNAP_STRATEGY": strategies[0]} if strategies[0] != "chevron" else {})}))
    all_rows: list[dict[str, Any]] = []
    try:
        for strategy in strategies:
            # the harness server may reach the loopback fixture server (SSRF guard opt-in)
            env = {"SUPERBROWSER_ALLOW_LOCAL_FIXTURES": "1"}
            if strategy != "chevron":
                env["SUPERBROWSER_SNAP_STRATEGY"] = strategy
            base = servers.ensure(env)
            client = Client(base)
            print(f"== strategy {strategy} ({len(items)} items x {len(conditions)} conditions) ==")
            rows = run_strategy(strategy, client, base_url, items, conditions=conditions)
            all_rows += rows
            report.write_csv(out / f"per_item__{strategy}.csv", rows)
            report.write_json(out / f"raw_results__{strategy}.json", {"generated_at": time.time(), "strategy": strategy, "rows": rows})
    finally:
        servers.close()
        httpd.shutdown()
    print(f"[e6] {len(all_rows)} trials -> {out}/per_item__<strategy>.csv; run analyze.py for the accuracy table")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
