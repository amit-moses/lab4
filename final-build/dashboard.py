#!/usr/bin/env python3
"""dashboard.py: live agent-traffic dashboard (stdlib only).

    python dashboard.py                      # proxy on 8041, UI on http://localhost:8050
    ANTHROPIC_BASE_URL=http://localhost:8041 claude

Imports proxy.py (SKIP list, --upstream/--port semantics) and records every
exchange in memory while forwarding it byte for byte. Headers are never stored.
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import proxy

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
STATE = {"epoch": 1, "rev": 0, "next_id": 1, "records": []}
UI_PORT = 8050


class Rec:
    def __init__(self, rid: int, method: str, path: str, body: bytes):
        self.id = rid
        self.method = method
        self.path = path
        self.req_raw = body.decode("utf-8", "replace")
        self.resp = ""
        self.status = None
        self.upstream_error = None
        self.t0 = time.time()
        self.t_first = None
        self.t_end = None
        self.done = False
        self.rev = 0
        self.cache: dict = {}


def bump(rec: Rec) -> None:
    with LOCK:
        STATE["rev"] += 1
        rec.rev = STATE["rev"]


def safe(fn, *a) -> None:
    """Recording must never break or delay the agent's call."""
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        try:
            sys.stderr.write(f"[dashboard] recording error: {e!r}\n")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- proxy side
class Recorder(proxy.Tap):
    def relay(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        rec = None
        try:
            with LOCK:
                rec = Rec(STATE["next_id"], self.command, self.path, body)
                STATE["next_id"] += 1
                STATE["records"].append(rec)
            bump(rec)
        except Exception:  # noqa: BLE001
            rec = None

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
            if rec:
                def fail():
                    rec.upstream_error = f"{type(e).__name__}: {e}"
                    rec.t_end = time.time()
                    rec.done = True
                    bump(rec)
                safe(fail)
            try:
                self.send_error(502, f"proxy: cannot reach {proxy.UPSTREAM}: {e}")
            except OSError:
                pass
            self.close_connection = True
            return

        def got_head():
            rec.status = reply.status
            rec.t_first = time.time()
            bump(rec)
        if rec:
            safe(got_head)
        print(f"[{proxy.stamp()}] #{rec.id if rec else '?'} {self.command} {self.path} -> {reply.status}", flush=True)
        try:
            self.send_response(reply.status, reply.reason)
            for name, value in reply.getheaders():
                if name.lower() not in proxy.SKIP and name.lower() != "content-encoding":
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                dec = codecs.getincrementaldecoder("utf-8")("replace")
                while chunk := reply.read1(4096):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if rec:
                        def add(c=chunk):
                            rec.resp += dec.decode(c)
                            bump(rec)
                        safe(add)
        except OSError:
            pass  # the agent hung up; keep what we have
        finally:
            if rec:
                def fin():
                    rec.t_end = time.time()
                    rec.done = True
                    bump(rec)
                safe(fin)
            self.close_connection = True
            try:
                upstream.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- analysis
def jloads(s):
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None


def jlen(v) -> int:
    return len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) if v is not None else 0


def req_info(rec: Rec) -> dict:
    c = rec.cache.get("req")
    if c is None:
        req = jloads(rec.req_raw) if rec.req_raw else None
        req = req if isinstance(req, dict) else None
        msgs = req.get("messages") if req else None
        msgs = msgs if isinstance(msgs, list) else []
        tools = req.get("tools") if req else None
        tools = tools if isinstance(tools, list) else []
        system = req.get("system") if req else None
        c = {
            "req": req, "msgs": msgs, "tools": tools, "system": system,
            "msgs_j": [json.dumps(m, sort_keys=True) for m in msgs],
            "tools_j": json.dumps(tools, sort_keys=True),
            "system_j": json.dumps(system, sort_keys=True),
            "tool_names": [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")],
            "sizes": {"system": jlen(system), "tools": jlen(tools or None), "messages": jlen(msgs or None),
                      "total": len(rec.req_raw.encode("utf-8"))},
            "model": req.get("model") if req else None,
        }
        rec.cache["req"] = c
    return c


def merge_usage(dst: dict, usage) -> None:
    if isinstance(usage, dict):
        for k, v in usage.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                dst[k] = v


def parse_reply(raw: str) -> dict:
    r = {"blocks": [], "model": None, "usage": {}, "stop": None, "errors": [], "error_msg": None, "format": None}
    if not raw.strip():
        return r
    if raw.lstrip().startswith(("event:", "data:", ":")):
        r["format"] = "sse"
        blocks: dict = {}
        for chunk in re.split(r"\r?\n\r?\n", raw):
            data = []
            for line in chunk.splitlines():
                if line.startswith("data:"):
                    d = line[5:]
                    data.append(d[1:] if d.startswith(" ") else d)
            if not data:
                continue
            ev = jloads("\n".join(data))
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            if t == "message_start":
                m = ev.get("message") or {}
                r["model"] = m.get("model") or r["model"]
                merge_usage(r["usage"], m.get("usage"))
            elif t == "content_block_start":
                cb = ev.get("content_block") or {}
                bt = cb.get("type")
                if bt == "text":
                    b = {"type": "text", "text": cb.get("text") or ""}
                elif bt == "thinking":
                    b = {"type": "thinking", "text": cb.get("thinking") or ""}
                elif bt == "tool_use":
                    b = {"type": "tool_use", "id": cb.get("id"), "name": cb.get("name"), "input_json": "",
                         "init": cb.get("input")}
                else:
                    b = {"type": str(bt), "raw": cb}
                blocks[ev.get("index", len(blocks))] = b
            elif t == "content_block_delta":
                b = blocks.get(ev.get("index"))
                d = ev.get("delta") or {}
                if b is not None:
                    dt = d.get("type")
                    if dt == "text_delta":
                        b["text"] = b.get("text", "") + (d.get("text") or "")
                    elif dt == "thinking_delta":
                        b["text"] = b.get("text", "") + (d.get("thinking") or "")
                    elif dt == "input_json_delta":
                        b["input_json"] = b.get("input_json", "") + (d.get("partial_json") or "")
            elif t == "message_delta":
                r["stop"] = (ev.get("delta") or {}).get("stop_reason") or r["stop"]
                merge_usage(r["usage"], ev.get("usage"))
            elif t == "error":
                e = ev.get("error")
                msg = e.get("message") if isinstance(e, dict) else None
                r["errors"].append(str(msg or e or "error event"))
        for k in sorted(blocks, key=lambda x: (not isinstance(x, int), x)):
            r["blocks"].append(blocks[k])
    else:
        obj = jloads(raw)
        if isinstance(obj, dict):
            if obj.get("type") == "message" or "content" in obj:
                r["format"] = "json"
                r["model"] = obj.get("model")
                r["stop"] = obj.get("stop_reason")
                merge_usage(r["usage"], obj.get("usage"))
                content = obj.get("content")
                if isinstance(content, str):
                    r["blocks"].append({"type": "text", "text": content})
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        r["blocks"].append({"type": "text", "text": b.get("text") or ""})
                    elif b.get("type") == "thinking":
                        r["blocks"].append({"type": "thinking", "text": b.get("thinking") or ""})
                    elif b.get("type") == "tool_use":
                        r["blocks"].append({"type": "tool_use", "id": b.get("id"), "name": b.get("name"),
                                            "input_json": None, "init": b.get("input")})
                    else:
                        r["blocks"].append({"type": str(b.get("type")), "raw": b})
            if obj.get("type") == "error" or isinstance(obj.get("error"), (dict, str)):
                r["format"] = r["format"] or "json"
                e = obj.get("error")
                r["error_msg"] = str(e.get("message") if isinstance(e, dict) and e.get("message") else e)
    for b in r["blocks"]:  # finalise tool inputs
        if b["type"] == "tool_use":
            ij = b.pop("input_json", None)
            init = b.pop("init", None)
            if ij:
                v = jloads(ij)
                if v is None:
                    b["input"] = None
                    b["input_text"] = ij
                else:
                    b["input"] = v
            else:
                b["input"] = init if init is not None else {}
    return r


def reply_info(rec: Rec) -> dict:
    c = rec.cache.get("reply")
    if c is None or c[0] != rec.rev:
        c = (rec.rev, parse_reply(rec.resp))
        rec.cache["reply"] = c
    return c[1]


RATES = {  # USD per million tokens: input, output, cache read, cache write (5 min TTL)
    "opus-old": (15.0, 75.0, 1.5, 18.75),
    "opus-new": (5.0, 25.0, 0.5, 6.25),
    "sonnet": (3.0, 15.0, 0.3, 3.75),
    "haiku-3": (0.8, 4.0, 0.08, 1.0),
    "haiku": (1.0, 5.0, 0.1, 1.25),
}


def rates_for(model):
    if not isinstance(model, str):
        return None
    m = model.lower()
    if "opus" in m:
        return RATES["opus-new"] if re.search(r"opus-(4-[5-9]|[5-9])", m) else RATES["opus-old"]
    if "sonnet" in m:
        return RATES["sonnet"]
    if "haiku" in m:
        return RATES["haiku-3"] if re.search(r"3-5-haiku|3-haiku|haiku-3", m) else RATES["haiku"]
    return None


def blocks_of(msg):
    c = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def analyze(recs: list) -> tuple[list, dict]:
    infos = {r.id: req_info(r) for r in recs}
    replies = {r.id: reply_info(r) for r in recs}
    call_rec: dict = {}   # tool_use id -> (reply record id)
    name_by_id: dict = {}
    carrier: dict = {}    # tool_use id -> (record id, is_error) of the FIRST request carrying the result
    for r in recs:
        for b in replies[r.id]["blocks"]:
            if b["type"] == "tool_use" and b.get("id"):
                call_rec.setdefault(b["id"], r.id)
                name_by_id[b["id"]] = b.get("name")
        for m in infos[r.id]["msgs"]:
            for b in blocks_of(m):
                if b.get("type") == "tool_use" and b.get("id"):
                    name_by_id.setdefault(b["id"], b.get("name"))
                elif b.get("type") == "tool_result" and b.get("tool_use_id"):
                    carrier.setdefault(b["tool_use_id"], (r.id, b.get("is_error") is True))

    out = []
    last_in_thread: dict = {}
    summary = {"requests": len(recs), "errors": 0, "paired_ok": 0, "failed": 0, "pending": 0}
    for r in recs:
        ri, rp = infos[r.id], replies[r.id]
        req = ri["req"]
        if r.method == "HEAD" or r.path.startswith("/api/hello"):
            kind, label = "connectivity", "connectivity check"
        elif r.method == "POST" and r.path.startswith("/v1/messages") and req is not None:
            if "count_tokens" in r.path:
                kind, label = "side", "side call: count_tokens"
            elif not ri["tools"]:
                kind, label = "side", "side call: no tools"
            else:
                kind, label = "main", "main conversation"
        else:
            kind, label = "other", "other request"

        thread = None
        if kind == "main":
            thread = "tools:" + hashlib.md5("|".join(ri["tool_names"]).encode()).hexdigest()
        elif kind == "side" and "count_tokens" not in r.path:
            thread = "notools:" + hashlib.md5(ri["system_j"][:200].encode()).hexdigest()

        new = {"mode": "none"}
        msgs = ri["msgs_j"]
        n = len(msgs)
        new_start = max(n - 1, 0)
        if thread:
            prev = last_in_thread.get(thread)
            if prev is None:
                new = {"mode": "first", "count": n}
            else:
                pi = infos[prev]
                pm = pi["msgs_j"]
                k = 0
                while k < len(pm) and k < n and pm[k] == msgs[k]:
                    k += 1
                if k < len(pm):
                    new = {"mode": "diverged"}
                else:
                    new = {"mode": "ext", "count": n - len(pm), "total": n}
                    new_start = len(pm) if n > len(pm) else n
                new["system_changed"] = pi["system_j"] != ri["system_j"]
                new["tools_changed"] = pi["tools_j"] != ri["tools_j"]
                new["prev"] = prev
            last_in_thread[thread] = r.id

        # tool pairing
        calls = []
        for b in rp["blocks"]:
            if b["type"] == "tool_use":
                cr = carrier.get(b.get("id"))
                st = "pending" if cr is None else ("failed" if cr[1] else "ok")
                calls.append({"id": b.get("id"), "name": b.get("name"), "status": st,
                              "carrier": cr[0] if cr else None})
                summary["pending" if st == "pending" else ("failed" if st == "failed" else "paired_ok")] += 1
        pair = {}
        carries = []
        for m in ri["msgs"]:
            for b in blocks_of(m):
                tid = b.get("id") if b.get("type") == "tool_use" else (
                    b.get("tool_use_id") if b.get("type") == "tool_result" else None)
                if not tid:
                    continue
                cr = carrier.get(tid)
                pair[tid] = {"name": name_by_id.get(tid), "call_rec": call_rec.get(tid),
                             "carrier": cr[0] if cr else None, "is_error": bool(cr and cr[1])}
                if b.get("type") == "tool_result":
                    first = cr is not None and cr[0] == r.id
                    carries.append({"id": tid, "name": name_by_id.get(tid), "is_error": b.get("is_error") is True,
                                    "first": first, "call_rec": call_rec.get(tid)})

        errors = []
        if r.upstream_error:
            errors.append(f"upstream connection failed: {r.upstream_error}")
        if r.status is not None and r.status >= 400:
            errors.append(f"HTTP {r.status}" + (f": {rp['error_msg']}" if rp["error_msg"] else ""))
        elif rp["error_msg"] and rp["format"] == "json":
            errors.append(f"error reply: {rp['error_msg']}")
        for c in carries:
            if c["first"] and c["is_error"]:
                errors.append(f"tool_result is_error: {c['name'] or 'unknown tool'} ({c['id']})")
        for e in rp["errors"]:
            errors.append(f"stream error: {e}")
        if errors:
            summary["errors"] += 1

        model = rp["model"] or ri["model"]
        u = rp["usage"]
        usage = {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                 "cache_read": u.get("cache_read_input_tokens"), "cache_write": u.get("cache_creation_input_tokens")}
        cost = None
        rates = rates_for(model)
        if rates and any(v is not None for v in usage.values()):
            cost = sum((usage[k] or 0) * rt for k, rt in zip(("input", "output", "cache_read", "cache_write"), rates)) / 1e6
        latency = (r.t_end - r.t0) if r.done and r.t_end else None
        ttfb = (r.t_first - r.t0) if r.t_first else None

        names = [c["name"] for c in calls if c["name"]] + [c["name"] for c in carries if c["first"] and c["name"]]
        d = {
            "id": r.id, "ts": r.t0, "method": r.method, "path": r.path, "status": r.status, "done": r.done,
            "kind": kind, "label": label, "thread": thread, "model": model, "usage": usage, "cost": cost,
            "cost_known": cost is not None, "latency": latency, "ttfb": ttfb, "sizes": ri["sizes"],
            "n_messages": len(ri["msgs"]), "n_tools": len(ri["tools"]), "new": new, "new_start": new_start,
            "calls": calls, "pair": pair, "carries": carries, "errors": errors, "tool_names": sorted(set(names)),
            "reply": {"blocks": rp["blocks"], "stop": rp["stop"], "model": rp["model"], "format": rp["format"]},
            "upstream_error": r.upstream_error, "resp_len": len(r.resp),
        }
        d["sig"] = hashlib.md5(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()
        out.append((r, d))
    return out, summary


# --------------------------------------------------------------------------- UI server
class UI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("host") or "").split(":")[0].lower()
        return host in ("localhost", "127.0.0.1", "[::1]", "")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urlsplit(self.path)
        if u.path in ("/", "/index.html", "/dashboard.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/records":
            q = parse_qs(u.query)
            since = int((q.get("since") or ["0"])[0] or 0)
            epoch = int((q.get("epoch") or ["0"])[0] or 0)
            with LOCK:
                recs = list(STATE["records"])
                rev, cur_epoch = STATE["rev"], STATE["epoch"]
            if epoch != cur_epoch:
                since = 0
            analysed, summary = analyze(recs)
            items = []
            for r, d in analysed:
                item = {"d": d, "rev": r.rev}
                if r.rev > since:
                    item["raw"] = {"req": r.req_raw, "resp": r.resp}
                items.append(item)
            body = json.dumps({"epoch": cur_epoch, "rev": rev, "summary": summary, "records": items}).encode()
            return self._send(200, body, "application/json")
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        if urlsplit(self.path).path == "/api/clear":
            with LOCK:
                STATE["records"].clear()
                STATE["epoch"] += 1
                STATE["rev"] += 1
            return self._send(200, b"{}", "application/json")
        self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent traffic proxy + live dashboard.")
    ap.add_argument("--port", type=int, default=8041, help="proxy port")
    ap.add_argument("--ui-port", type=int, default=8050, help="dashboard port")
    ap.add_argument("--upstream", default=proxy.UPSTREAM)
    args = ap.parse_args()
    proxy.UPSTREAM = args.upstream.rstrip("/")
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    ui = ThreadingHTTPServer(("127.0.0.1", args.ui_port), UI)
    ui.daemon_threads = True
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Recorder)
    srv.daemon_threads = True
    print(f"proxy on http://localhost:{args.port} -> {proxy.UPSTREAM}")
    print(f"dashboard on http://localhost:{args.ui_port}")
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{args.port} claude", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
