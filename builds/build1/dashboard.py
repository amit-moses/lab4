#!/usr/bin/env python3
"""dashboard.py: live traffic dashboard on top of proxy.py (stdlib only).

    python dashboard.py                      # proxy on :8041, dashboard on http://localhost:8050
    ANTHROPIC_BASE_URL=http://localhost:8041 claude

Runs the same forwarding logic as proxy.py (headers and body forwarded unchanged,
reply streamed back byte for byte) but records each exchange in memory and serves
a single-file UI (dashboard.html) that polls /api/list every second.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import proxy  # reuse SKIP header set and stamp(); proxy.py itself is untouched

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
RECORDS: list[dict] = []          # index == id - 1
MAX_RAW = 20_000_000

# USD per million tokens (input, output). Estimates only.
PRICES = [("opus", 15.0, 75.0), ("sonnet", 3.0, 15.0), ("haiku", 1.0, 5.0)]


def price_for(model: str):
    m = (model or "").lower()
    for k, i, o in PRICES:
        if k in m:
            return i, o
    return None


def parse_response(raw: str) -> dict:
    """Turn a JSON or SSE reply into {blocks, usage, stop_reason, error, model}."""
    out = {"blocks": [], "usage": {}, "stop_reason": None, "error": None, "model": None}
    raw = raw.strip()
    if not raw:
        return out
    if not (raw.startswith("event:") or raw.startswith("data:")):
        try:
            j = json.loads(raw)
        except ValueError:
            out["error"] = raw[:2000]
            return out
        if j.get("type") == "error" or "error" in j:
            out["error"] = json.dumps(j.get("error", j), ensure_ascii=False)
        out["blocks"] = j.get("content") or []
        out["usage"] = j.get("usage") or {}
        out["stop_reason"] = j.get("stop_reason")
        out["model"] = j.get("model")
        return out
    blocks: dict[int, dict] = {}
    jsonbuf: dict[int, str] = {}
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        t = ev.get("type")
        if t == "message_start":
            msg = ev.get("message", {})
            out["model"] = msg.get("model")
            out["usage"].update(msg.get("usage") or {})
        elif t == "content_block_start":
            b = dict(ev.get("content_block") or {})
            blocks[ev["index"]] = b
            jsonbuf[ev["index"]] = ""
        elif t == "content_block_delta":
            b = blocks.setdefault(ev["index"], {"type": "text", "text": ""})
            d = ev.get("delta", {})
            if d.get("type") == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif d.get("type") == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif d.get("type") == "input_json_delta":
                jsonbuf[ev["index"]] = jsonbuf.get(ev["index"], "") + d.get("partial_json", "")
        elif t == "message_delta":
            out["usage"].update(ev.get("usage") or {})
            out["stop_reason"] = (ev.get("delta") or {}).get("stop_reason") or out["stop_reason"]
        elif t == "error":
            out["error"] = json.dumps(ev.get("error", ev), ensure_ascii=False)
    for i, b in blocks.items():
        if b.get("type") == "tool_use":
            s = jsonbuf.get(i, "")
            try:
                b["input"] = json.loads(s) if s else b.get("input", {})
            except ValueError:
                b["input"] = {"_partial_json": s}
    out["blocks"] = [blocks[i] for i in sorted(blocks)]
    return out


def hsh(x) -> str:
    return hashlib.md5(json.dumps(x, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]


def content_blocks(msg: dict) -> list:
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return c or []


def analyse(rec: dict) -> None:
    """Derive summary fields (called under LOCK, after body / response changes)."""
    req = rec["req"] if isinstance(rec["req"], dict) else {}
    rec["model"] = req.get("model") or rec["resp"].get("model")
    msgs = req.get("messages") or []
    rec["n_messages"] = len(msgs)
    rec["sys_hash"] = hsh(req.get("system"))
    rec["tools_hash"] = hsh(req.get("tools"))
    rec["n_tools"] = len(req.get("tools") or [])
    # tool results carried by this request
    results = {}
    for m in msgs:
        for b in content_blocks(m):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = b
    rec["results_in"] = results
    rec["calls_out"] = [b for b in rec["resp"]["blocks"] if b.get("type") == "tool_use"]
    u = rec["resp"].get("usage") or {}
    rec["usage"] = {
        "in": u.get("input_tokens", 0) or 0,
        "out": u.get("output_tokens", 0) or 0,
        "cache_read": u.get("cache_read_input_tokens", 0) or 0,
        "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
    }
    p = price_for(rec["model"] or "")
    if p and u:
        us = rec["usage"]
        rec["cost"] = (us["in"] * p[0] + us["out"] * p[1] + us["cache_read"] * p[0] * 0.1
                       + us["cache_write"] * p[0] * 1.25) / 1e6
    else:
        rec["cost"] = None


def tool_status(tid: str, upto_id: int) -> dict:
    """Result for tool_use id `tid`, searched in later requests."""
    for r in RECORDS[upto_id:]:
        b = r.get("results_in", {}).get(tid)
        if b is not None:
            return {"found": True, "in_request": r["id"], "is_error": bool(b.get("is_error")), "block": b}
    return {"found": False}


def is_error(rec: dict) -> bool:
    if rec.get("status") and rec["status"] >= 400:
        return True
    if rec["resp"].get("error") or rec.get("conn_error"):
        return True
    for c in rec.get("calls_out", []):
        if tool_status(c.get("id"), rec["id"]).get("is_error"):
            return True
    return False


def summary(rec: dict) -> dict:
    prev = None
    for r in reversed(RECORDS[: rec["id"] - 1]):
        if r.get("model") and r["path"] == rec["path"] and r["n_messages"]:
            prev = r
            break
    diff = None
    if prev:
        diff = {"vs": prev["id"], "new_messages": rec["n_messages"] - prev["n_messages"],
                "system_changed": prev["sys_hash"] != rec["sys_hash"],
                "tools_changed": prev["tools_hash"] != rec["tools_hash"]}
    last_results = []
    msgs = rec["req"].get("messages") if isinstance(rec["req"], dict) else None
    if msgs:
        for b in content_blocks(msgs[-1]):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                last_results.append(b.get("tool_use_id"))
    calls = []
    for c in rec.get("calls_out", []):
        st = tool_status(c.get("id"), rec["id"])
        calls.append({"id": c.get("id"), "name": c.get("name"), "found": st["found"],
                      "is_error": st.get("is_error", False), "in_request": st.get("in_request")})
    return {
        "id": rec["id"], "t": rec["t"], "method": rec["method"], "path": rec["path"],
        "status": rec.get("status"), "done": rec["done"], "model": rec.get("model"),
        "latency": rec.get("latency"), "ttfb": rec.get("ttfb"), "usage": rec.get("usage"),
        "cost": rec.get("cost"), "n_messages": rec["n_messages"], "n_tools": rec["n_tools"],
        "calls": calls, "stop_reason": rec["resp"].get("stop_reason"),
        "error": is_error(rec), "diff": diff, "n_results": len(last_results),
        "result_tools": sorted({n for r in RECORDS[: rec["id"] - 1] for c in r.get("calls_out", [])
                                for n in [c.get("name")] if c.get("id") in last_results}),
        "result_errors": sum(1 for i in last_results if rec["results_in"].get(i, {}).get("is_error")),
    }


def detail(rec: dict) -> dict:
    s = summary(rec)
    prev_n = None
    if s["diff"]:
        prev_n = RECORDS[s["diff"]["vs"] - 1]["n_messages"]
    # name lookup for tool_use ids seen so far (for labelling results)
    names = {}
    for r in RECORDS[: rec["id"]]:
        msgs = r["req"].get("messages") if isinstance(r["req"], dict) else []
        for m in msgs or []:
            for b in content_blocks(m):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b.get("id")] = b.get("name")
        for b in r["resp"]["blocks"]:
            if b.get("type") == "tool_use":
                names[b.get("id")] = b.get("name")
    calls = []
    for c in rec.get("calls_out", []):
        st = tool_status(c.get("id"), rec["id"])
        calls.append({"call": c, "result": st})
    # where each tool_use in this request's history got its result (within this request)
    return {"summary": s, "request": rec["req"], "response": rec["resp"], "calls": calls,
            "names": names, "prev_n": prev_n, "status": rec.get("status"),
            "conn_error": rec.get("conn_error"), "raw_len": len(rec["raw"])}


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    do_POST = do_GET = do_HEAD = lambda self: self.relay()

    def relay(self):
        t0 = time.time()
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            req = json.loads(body) if body else {}
        except ValueError:
            req = {"_raw": body.decode("utf-8", "replace")}
        with LOCK:
            rec = {"id": len(RECORDS) + 1, "t": time.strftime("%H:%M:%S"), "ts": t0,
                   "method": self.command, "path": self.path, "req": req, "raw": "",
                   "resp": parse_response(""), "done": False}
            RECORDS.append(rec)
            analyse(rec)
        print(f"[{proxy.stamp()}] #{rec['id']} {self.command} {self.path}", flush=True)

        target = urlsplit(proxy.UPSTREAM)
        cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        up = cls(target.netloc, timeout=10)
        try:
            up.connect()
        except OSError as e:
            with LOCK:
                rec["conn_error"] = str(e)
                rec["status"] = 502
                rec["done"] = True
                rec["latency"] = time.time() - t0
            self.send_error(502, f"proxy: cannot reach {proxy.UPSTREAM}: {e}")
            self.close_connection = True
            return
        up.sock.settimeout(600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in proxy.SKIP}
        up.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
        reply = up.getresponse()
        with LOCK:
            rec["status"] = reply.status
            rec["ttfb"] = time.time() - t0
        self.send_response(reply.status, reply.reason)
        for n, v in reply.getheaders():
            if n.lower() not in proxy.SKIP and n.lower() != "content-encoding":
                self.send_header(n, v)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            self.close_connection = True
            with LOCK:
                rec["done"] = True
            return
        buf = bytearray()
        last = 0.0
        try:
            while chunk := reply.read1(4096):
                self.wfile.write(chunk)
                self.wfile.flush()
                if len(buf) < MAX_RAW:
                    buf += chunk
                now = time.time()
                if now - last > 0.5:  # live partial parse, throttled
                    last = now
                    with LOCK:
                        rec["raw"] = buf.decode("utf-8", "replace")
                        rec["resp"] = parse_response(rec["raw"])
                        rec["latency"] = now - t0
                        analyse(rec)
        finally:
            with LOCK:
                rec["raw"] = buf.decode("utf-8", "replace")
                rec["resp"] = parse_response(rec["raw"])
                rec["latency"] = time.time() - t0
                rec["done"] = True
                analyse(rec)
            self.close_connection = True
        print(f"[{proxy.stamp()}] #{rec['id']} done {reply.status} {rec['latency']:.1f}s", flush=True)


class UI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, ctype, data: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self.send(200, "text/html; charset=utf-8", f.read())
        if u.path == "/api/list":
            with LOCK:
                out = [summary(r) for r in RECORDS]
            return self.send(200, "application/json", json.dumps(out, ensure_ascii=False).encode())
        if u.path.startswith("/api/req/"):
            try:
                i = int(u.path.rsplit("/", 1)[1])
                with LOCK:
                    out = detail(RECORDS[i - 1])
            except (ValueError, IndexError):
                return self.send(404, "text/plain", b"not found")
            return self.send(200, "application/json", json.dumps(out, ensure_ascii=False, default=str).encode())
        if u.path == "/api/clear":
            with LOCK:
                RECORDS.clear()
            return self.send(200, "application/json", b"{}")
        self.send(404, "text/plain", b"not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8041, help="proxy port")
    ap.add_argument("--ui-port", type=int, default=8050, help="dashboard port")
    ap.add_argument("--upstream", default=proxy.UPSTREAM)
    a = ap.parse_args()
    proxy.UPSTREAM = a.upstream.rstrip("/")
    sys.stdout.reconfigure(errors="replace")
    ui = ThreadingHTTPServer(("127.0.0.1", a.ui_port), UI)
    ui.daemon_threads = True
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Tap)
    srv.daemon_threads = True
    print(f"proxy     http://localhost:{a.port}  ->  {proxy.UPSTREAM}")
    print(f"dashboard http://localhost:{a.ui_port}")
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{a.port} claude")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
