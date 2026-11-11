#!/usr/bin/env python3
"""dashboard.py: proxy + live traffic dashboard (stdlib only).

    python dashboard.py                       # proxy on :8041, UI on http://localhost:8050
    ANTHROPIC_BASE_URL=http://localhost:8041 claude
    python dashboard.py --upstream http://127.0.0.1:9999 --port 8041 --ui-port 8050

Reuses proxy.py (SKIP list, UPSTREAM default). Forwarding is byte for byte; recording is
wrapped so it can never break or delay the agent's call. Records live in memory only.
Headers are never stored, so authorization / x-api-key values cannot reach the browser.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import proxy

HERE = os.path.dirname(os.path.abspath(__file__))

# Hard-coded estimates, USD per million tokens (input, output). First substring match wins.
# Cache read = 0.1 x input, cache write = 1.25 x input. Unknown model -> cost "n/a".
RATES = [
    ("opus-4-5", 5, 25), ("opus-4-6", 5, 25), ("opus-4-7", 5, 25),
    ("opus", 15, 75),
    ("sonnet", 3, 15),
    ("haiku-4-5", 1, 5), ("haiku", 0.8, 4),
]
BODY_CAP = 5_000_000


# ----------------------------------------------------------------------------- store
class Rec:
    def __init__(self, rid: int, method: str, path: str, body: bytes) -> None:
        self.id = rid
        self.method = method
        self.path = path
        self.t_wall = time.strftime("%H:%M:%S")
        self.t_start = time.time()
        self.t_first = None
        self.t_end = None
        self.status = None
        self.reason = ""
        self.conn_error = None       # upstream failure text
        self.stream_errors = []      # error events in stream
        self.sse = False
        self.done = False
        self.model = None
        self.usage = {}
        self.blocks = {}             # index -> block (stream)
        self.json_resp = None        # parsed non-stream reply
        self.raw_resp = b""
        self.buf = b""
        self.req = None
        self.req_raw = None
        try:
            self.req = json.loads(body) if body else None
        except ValueError:
            self.req_raw = body[:20000].decode("utf-8", "replace")

    # -- feeding the reply
    def begin(self, status: int, reason: str, ctype: str) -> None:
        self.status, self.reason = status, reason
        self.sse = "text/event-stream" in (ctype or "")

    def feed(self, chunk: bytes) -> None:
        if self.t_first is None:
            self.t_first = time.time()
        if not self.sse:
            if len(self.raw_resp) < BODY_CAP:
                self.raw_resp += chunk
            return
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                try:
                    self.event(json.loads(line[5:].strip()))
                except Exception:
                    pass

    def event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "message_start":
            m = ev.get("message") or {}
            self.model = m.get("model") or self.model
            self.usage.update({k: v for k, v in (m.get("usage") or {}).items() if isinstance(v, (int, float))})
        elif t == "content_block_start":
            b = dict(ev.get("content_block") or {})
            if b.get("type") == "tool_use":
                b["_json"] = ""
            elif b.get("type") == "text":
                b.setdefault("text", "")
            elif b.get("type") == "thinking":
                b.setdefault("thinking", "")
            self.blocks[ev.get("index", len(self.blocks))] = b
        elif t == "content_block_delta":
            b = self.blocks.get(ev.get("index"))
            d = ev.get("delta") or {}
            if b is not None:
                if d.get("type") == "text_delta":
                    b["text"] = b.get("text", "") + d.get("text", "")
                elif d.get("type") == "input_json_delta":
                    b["_json"] = b.get("_json", "") + d.get("partial_json", "")
                elif d.get("type") == "thinking_delta":
                    b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
        elif t == "message_delta":
            self.usage.update({k: v for k, v in (ev.get("usage") or {}).items() if isinstance(v, (int, float))})
        elif t == "error":
            e = ev.get("error") or {}
            self.stream_errors.append(f"{e.get('type', 'error')}: {e.get('message', '')}".strip())

    def finish(self) -> None:
        try:
            if not self.sse and self.raw_resp:
                try:
                    self.json_resp = json.loads(self.raw_resp)
                except ValueError:
                    pass
                if isinstance(self.json_resp, dict):
                    self.model = self.json_resp.get("model") or self.model
                    u = self.json_resp.get("usage") or {}
                    self.usage.update({k: v for k, v in u.items() if isinstance(v, (int, float))})
            self.t_end = time.time()
            self.done = True
        except Exception:
            self.done = True

    # -- views
    def output_blocks(self) -> list:
        if self.sse:
            out = []
            for i in sorted(self.blocks):
                b = {k: v for k, v in self.blocks[i].items() if k != "_json"}
                if b.get("type") == "tool_use":
                    raw = self.blocks[i].get("_json", "")
                    try:
                        b["input"] = json.loads(raw) if raw else b.get("input", {})
                    except ValueError:
                        b["input"] = {"_partial_json": raw}
                out.append(b)
            return out
        if isinstance(self.json_resp, dict) and isinstance(self.json_resp.get("content"), list):
            return self.json_resp["content"]
        return []

    def error_body(self) -> str:
        if self.status is not None and self.status >= 400 and self.raw_resp:
            try:
                j = json.loads(self.raw_resp)
                e = j.get("error") if isinstance(j, dict) else None
                if isinstance(e, dict):
                    return str(e.get("message") or e.get("type") or "")
            except ValueError:
                pass
            return self.raw_resp[:300].decode("utf-8", "replace")
        return ""


LOCK = threading.Lock()
RECS: list[Rec] = []
NEXT_ID = [1]


def new_rec(method: str, path: str, body: bytes) -> Rec | None:
    try:
        with LOCK:
            r = Rec(NEXT_ID[0], method, path, body)
            NEXT_ID[0] += 1
            RECS.append(r)
            return r
    except Exception:
        return None


# ----------------------------------------------------------------------------- analysis
def blocks_of(msg: dict) -> list:
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def result_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) and x.get("type") == "text"
                         else json.dumps(x, ensure_ascii=False) for x in c)
    return json.dumps(c, ensure_ascii=False) if c is not None else ""


def msgs_of(r: Rec) -> list:
    return r.req.get("messages") if isinstance(r.req, dict) and isinstance(r.req.get("messages"), list) else []


def cost_of(model, u):
    if not model or not u:
        return None
    for key, rin, rout in RATES:
        if key in model:
            return (u.get("input_tokens", 0) * rin + u.get("output_tokens", 0) * rout
                    + u.get("cache_read_input_tokens", 0) * rin * 0.1
                    + u.get("cache_creation_input_tokens", 0) * rin * 1.25) / 1e6
    return None


def analyze(recs: list) -> tuple[list, dict]:
    """Returns (summaries oldest->newest, global link maps)."""
    emitted: dict = {}   # tool_use id -> (seq, name)
    carried: dict = {}   # tool_use_id -> dict(seq, is_error, text)
    for r in recs:
        for b in r.output_blocks():
            if b.get("type") == "tool_use" and b.get("id"):
                emitted.setdefault(b["id"], (r.id, b.get("name", "?")))
        for m in msgs_of(r):
            for b in blocks_of(m):
                if b.get("type") == "tool_result" and b.get("tool_use_id"):
                    carried.setdefault(b["tool_use_id"], {
                        "seq": r.id, "is_error": bool(b.get("is_error")),
                        "text": result_text(b.get("content"))[:300]})
                if b.get("type") == "tool_use" and b.get("id"):
                    emitted.setdefault(b["id"], (None, b.get("name", "?")))

    sums = []
    prev = None
    for r in recs:
        msgs = msgs_of(r)
        errors = []
        if r.conn_error:
            errors.append("upstream connection failed: " + r.conn_error)
        elif r.status is not None and r.status >= 400:
            eb = r.error_body()
            errors.append(f"HTTP {r.status}" + (": " + eb if eb else ""))
        for e in r.stream_errors:
            errors.append("stream error: " + e)
        tools = []
        for m in msgs:
            for b in blocks_of(m):
                if b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    nm = emitted.get(tid, (None, "?"))[1]
                    if carried.get(tid, {}).get("seq") == r.id:
                        tools.append(nm)
                        if b.get("is_error"):
                            errors.append(f"tool_result error from {nm} ({tid})")
        calls = []
        text = ""
        for b in r.output_blocks():
            if b.get("type") == "tool_use":
                c = carried.get(b.get("id"))
                state = "pending" if not c else ("failed" if c["is_error"] else "ok")
                calls.append({"name": b.get("name", "?"), "state": state, "result_seq": c["seq"] if c else None})
                tools.append(b.get("name", "?"))
            elif b.get("type") == "text" and not text:
                text = b.get("text", "")
        if not text and errors:
            text = errors[0]
        # what is new since previous request
        new_start, sys_changed, tools_changed, diverged = 0, False, False, False
        if prev is not None and isinstance(r.req, dict):
            pm = msgs_of(prev)
            while new_start < min(len(pm), len(msgs)) and pm[new_start] == msgs[new_start]:
                new_start += 1
            diverged = new_start < len(pm)
            sys_changed = r.req.get("system") != prev.req.get("system")
            tools_changed = r.req.get("tools") != prev.req.get("tools")
        u = r.usage
        end = r.t_end or time.time()
        c = cost_of(r.model, u)
        sums.append({
            "id": r.id, "time": r.t_wall, "method": r.method, "path": r.path, "status": r.status,
            "done": r.done, "streaming": bool(r.sse and not r.done),
            "model": r.model or (r.req.get("model") if isinstance(r.req, dict) else None),
            "in": u.get("input_tokens"), "out": u.get("output_tokens"),
            "cache_read": u.get("cache_read_input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
            "latency_ms": int((end - r.t_start) * 1000), "ttfb_ms": int((r.t_first - r.t_start) * 1000) if r.t_first else None,
            "cost": c, "errors": errors, "tools": sorted(set(tools)), "calls": calls,
            "n_messages": len(msgs), "new_start": new_start, "new_count": len(msgs) - new_start,
            "first": prev is None, "sys_changed": sys_changed, "tools_changed": tools_changed, "diverged": diverged,
            "preview": text[:160],
        })
        if isinstance(r.req, dict):
            prev = r
    return sums, {"emitted": emitted, "carried": carried}


def detail(rid: int):
    with LOCK:
        recs = list(RECS)
    r = next((x for x in recs if x.id == rid), None)
    if r is None:
        return None
    sums, maps = analyze(recs)
    s = next(x for x in sums if x["id"] == rid)
    ids = set()
    for m in msgs_of(r):
        for b in blocks_of(m):
            ids.add(b.get("id") if b.get("type") == "tool_use" else b.get("tool_use_id") if b.get("type") == "tool_result" else None)
    out = r.output_blocks()
    ids.update(b.get("id") for b in out if b.get("type") == "tool_use")
    links = {}
    for i in ids:
        if not i:
            continue
        e, c = maps["emitted"].get(i), maps["carried"].get(i)
        links[i] = {"use_seq": e[0] if e else None, "name": e[1] if e else "?",
                    "res_seq": c["seq"] if c else None, "res_error": c["is_error"] if c else None,
                    "res_text": c["text"] if c else None}
    return {"summary": s, "req": r.req, "req_raw": r.req_raw, "output": out, "links": links,
            "resp_raw": (r.raw_resp[:20000].decode("utf-8", "replace") if not out and not r.sse else ""),
            "stop_reason": (r.json_resp or {}).get("stop_reason") if isinstance(r.json_resp, dict) else None}


# ----------------------------------------------------------------------------- proxy handler
class Rec_Tap(proxy.Tap):
    def relay(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        rec = None
        try:
            rec = new_rec(self.command, self.path, body)
            print(f"[{proxy.stamp()}] #{rec.id if rec else '?'} {self.command} {self.path}", flush=True)
        except Exception:
            pass

        def note(fn, *a):
            try:
                if rec is not None:
                    fn(*a)
            except Exception:
                pass

        target = urlsplit(proxy.UPSTREAM)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
            upstream.sock.settimeout(600)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in proxy.SKIP}
            upstream.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
            reply = upstream.getresponse()
        except OSError as e:
            if rec is not None:
                rec.conn_error = str(e)
                rec.status = 502
            note(rec.finish) if rec else None
            self.send_error(502, f"proxy: cannot reach {proxy.UPSTREAM}: {e}")
            self.close_connection = True
            return

        note(rec.begin, reply.status, reply.reason, reply.getheader("content-type") or "")
        try:
            self.send_response(reply.status, reply.reason)
            for name, value in reply.getheaders():
                if name.lower() not in proxy.SKIP and name.lower() != "content-encoding":
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while chunk := reply.read1(4096):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    note(rec.feed, chunk)
        except OSError as e:
            if rec is not None and not isinstance(e, (BrokenPipeError, ConnectionResetError)):
                rec.conn_error = "stream broke: " + str(e)
        finally:
            note(rec.finish)
            self.close_connection = True


# ----------------------------------------------------------------------------- UI server
class UI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        pass

    def send(self, code: int, ctype: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        u = urlsplit(self.path)
        try:
            if u.path in ("/", "/index.html", "/dashboard.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    self.send(200, "text/html; charset=utf-8", f.read())
            elif u.path == "/api/summary":
                with LOCK:
                    recs = list(RECS)
                sums, _ = analyze(recs)
                self.send(200, "application/json", json.dumps({"records": sums}).encode())
            elif u.path == "/api/record":
                d = detail(int(parse_qs(u.query).get("id", ["0"])[0]))
                if d is None:
                    self.send(404, "application/json", b"{}")
                else:
                    self.send(200, "application/json", json.dumps(d, ensure_ascii=False).encode())
            else:
                self.send(404, "text/plain", b"not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send(500, "text/plain", str(e).encode())

    def do_POST(self) -> None:
        if urlsplit(self.path).path == "/api/clear":
            with LOCK:
                RECS.clear()
            self.send(200, "application/json", b"{}")
        else:
            self.send(404, "text/plain", b"not found")


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent traffic proxy + live dashboard.")
    ap.add_argument("--port", type=int, default=8041, help="proxy port")
    ap.add_argument("--ui-port", type=int, default=8050, help="dashboard port")
    ap.add_argument("--upstream", default=proxy.UPSTREAM)
    a = ap.parse_args()
    proxy.UPSTREAM = a.upstream.rstrip("/")
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    ui = ThreadingHTTPServer(("127.0.0.1", a.ui_port), UI)
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Rec_Tap)
    print(f"proxy http://localhost:{a.port} -> {proxy.UPSTREAM}\ndashboard http://localhost:{a.ui_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
