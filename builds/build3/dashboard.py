#!/usr/bin/env python3
"""dashboard.py: agent traffic monitor. Proxy on 8041 (reuses proxy.py), UI on 8050.

    python dashboard.py [--port 8041] [--ui-port 8050] [--upstream https://api.anthropic.com]
    ANTHROPIC_BASE_URL=http://localhost:8041 claude        # then open http://localhost:8050

Forwarding is byte for byte (same rules as proxy.py). Recording is best effort and
wrapped in try/except so it can never break or delay the agent's call.
Header values are never stored, so they can never reach the browser.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import proxy

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_RAW = 32 * 1024 * 1024
# (substring of model id, $ per million input, $ per million output). Estimates.
RATES = [("opus", 15.0, 75.0), ("sonnet", 3.0, 15.0), ("haiku", 1.0, 5.0)]
COST_NOTE = ("estimate: cache write billed at the 5-minute rate (1.25x input; 2x for a 1-hour TTL), "
             "cache read at 0.1x input; list prices hard-coded")


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True)


def h(s: str) -> str:
    return hashlib.md5(s.encode("utf-8", "replace")).hexdigest()[:12]


def flat_text(c) -> str:
    """Flatten tool_result / system content to plain text."""
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(str(b.get("text", "")))
                elif b.get("type") == "image":
                    out.append("[image]")
                else:
                    out.append(jdump(b))
            else:
                out.append(str(b))
        return "\n".join(out)
    return jdump(c)


def norm_message(m) -> dict:
    if not isinstance(m, dict):
        return {"role": "?", "kind": "other", "blocks": [{"type": "other", "json": jdump(m)}]}
    role = str(m.get("role", "?"))
    c = m.get("content")
    if isinstance(c, str):
        c = [{"type": "text", "text": c}]
    elif not isinstance(c, list):
        c = []
    blocks = []
    for b in c:
        if not isinstance(b, dict):
            blocks.append({"type": "other", "json": jdump(b)})
            continue
        t = b.get("type")
        if t == "text":
            txt = str(b.get("text", ""))
            blocks.append({"type": "reminder" if txt.lstrip().startswith("<system-reminder>") else "text", "text": txt})
        elif t == "tool_use":
            blocks.append({"type": "tool_use", "id": b.get("id", ""), "name": b.get("name", ""),
                           "input": json.dumps(b.get("input"), indent=2, ensure_ascii=False)})
        elif t == "tool_result":
            blocks.append({"type": "tool_result", "id": b.get("tool_use_id", ""),
                           "is_error": bool(b.get("is_error")), "text": flat_text(b.get("content"))})
        elif t in ("thinking", "redacted_thinking"):
            blocks.append({"type": "thinking", "text": str(b.get("thinking", "(redacted)"))})
        else:
            blocks.append({"type": "other", "json": json.dumps(b, indent=2, ensure_ascii=False)})
    kind = "system" if role == "system" else role
    return {"role": role, "kind": kind, "blocks": blocks}


def analyse_request(method: str, path: str, body: bytes) -> dict:
    a = {"json": False, "model": "", "has_tools": False, "tool_defs": [], "system": "", "messages": [],
         "msg_hashes": [], "sys_hash": "", "tools_hash": "", "sizes": {"system": 0, "tools": 0, "messages": 0, "total": len(body)},
         "results": [], "thread": ""}
    if not body:
        return a
    try:
        d = json.loads(body)
    except ValueError:
        return a
    if not isinstance(d, dict):
        return a
    a["json"] = True
    a["model"] = str(d.get("model", "") or "")
    sysv = d.get("system")
    a["system"] = flat_text(sysv)
    tools = d.get("tools") if isinstance(d.get("tools"), list) else []
    msgs = d.get("messages") if isinstance(d.get("messages"), list) else []
    a["tool_defs"] = tools
    a["has_tools"] = bool(tools)
    a["messages"] = msgs
    a["msg_hashes"] = [h(jdump(m)) for m in msgs]
    a["sys_hash"] = h(jdump(sysv))
    a["tools_hash"] = h(jdump(tools))
    a["sizes"].update(system=len(jdump(sysv).encode()) if sysv else 0,
                      tools=len(jdump(tools).encode()) if tools else 0,
                      messages=len(jdump(msgs).encode()) if msgs else 0)
    names = sorted(str(t.get("name", "")) for t in tools if isinstance(t, dict))
    a["thread"] = ("T:" + h("|".join(names))) if names else ("N:" + a["sys_hash"])
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    a["results"].append({"id": str(b.get("tool_use_id", "")), "error": bool(b.get("is_error")),
                                         "text": flat_text(b.get("content")), "msg": i})
    return a


def parse_reply(raw: bytes, status: int) -> dict:
    r = {"text": "", "thinking": "", "tools": [], "stop": "", "error": "", "model": "",
         "usage": {"in": None, "out": None, "cr": None, "cw": None}}
    text = raw.decode("utf-8", "replace")

    def use(u):
        if not isinstance(u, dict):
            return
        for k, f in (("in", "input_tokens"), ("out", "output_tokens"),
                     ("cr", "cache_read_input_tokens"), ("cw", "cache_creation_input_tokens")):
            if isinstance(u.get(f), (int, float)):
                r["usage"][k] = int(u[f])

    def finish_tool(t):
        if t.get("buf") is not None:
            s = t.pop("buf")
            if s.strip():
                try:
                    t["input"] = json.dumps(json.loads(s), indent=2, ensure_ascii=False)
                except ValueError:
                    t["input"] = s
            elif "input" not in t:
                t["input"] = "{}"
        t.setdefault("input", "{}")

    if re.search(r"^(event|data):", text, re.M):
        blocks = {}
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            if t == "message_start":
                m = ev.get("message") or {}
                r["model"] = str(m.get("model", "") or r["model"])
                use(m.get("usage"))
            elif t == "content_block_start":
                cb = ev.get("content_block") or {}
                idx = ev.get("index", len(blocks))
                if cb.get("type") == "tool_use":
                    blocks[idx] = {"id": cb.get("id", ""), "name": cb.get("name", ""), "buf": ""}
                    if cb.get("input"):
                        blocks[idx]["input"] = json.dumps(cb["input"], indent=2, ensure_ascii=False)
                    r["tools"].append(blocks[idx])
                if cb.get("type") == "text" and cb.get("text"):
                    r["text"] += str(cb["text"])
            elif t == "content_block_delta":
                dl = ev.get("delta") or {}
                dt = dl.get("type")
                if dt == "text_delta":
                    r["text"] += str(dl.get("text", ""))
                elif dt == "thinking_delta":
                    r["thinking"] += str(dl.get("thinking", ""))
                elif dt == "input_json_delta":
                    b = blocks.get(ev.get("index"))
                    if b is not None:
                        b["buf"] = (b.get("buf") or "") + str(dl.get("partial_json", ""))
            elif t == "message_delta":
                r["stop"] = str((ev.get("delta") or {}).get("stop_reason", "") or r["stop"])
                use(ev.get("usage"))
            elif t == "error":
                e = ev.get("error")
                r["error"] = (str(e.get("type", "")) + ": " + str(e.get("message", ""))) if isinstance(e, dict) else str(e)
    else:
        try:
            d = json.loads(text)
        except ValueError:
            d = None
        if isinstance(d, dict):
            if isinstance(d.get("error"), dict):
                e = d["error"]
                r["error"] = (str(e.get("type", "")) + ": " + str(e.get("message", ""))).strip(": ")
            elif d.get("type") == "error":
                r["error"] = str(d.get("message", "error"))
            r["model"] = str(d.get("model", "") or "")
            r["stop"] = str(d.get("stop_reason", "") or "")
            use(d.get("usage"))
            for b in d.get("content") or []:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        r["text"] += str(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        r["tools"].append({"id": b.get("id", ""), "name": b.get("name", ""),
                                           "input": json.dumps(b.get("input"), indent=2, ensure_ascii=False)})
        elif text.strip() and status >= 400:
            r["error"] = text.strip()[:500]
        elif text.strip():
            r["text"] = text
    for t in r["tools"]:
        finish_tool(t)
    return r


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.recs: list[dict] = []
        self.next_id = 1

    def start(self, method: str, path: str, body: bytes) -> dict:
        rec = {"id": 0, "t0": time.time(), "method": method, "path": path, "status": 0, "done": False,
               "latency": None, "ttfb": None, "conn_error": "", "raw": bytearray(), "req": analyse_request(method, path, body),
               "cache": (-1, None)}
        with self.lock:
            rec["id"] = self.next_id
            self.next_id += 1
            self.recs.append(rec)
        return rec

    def status(self, rec, status):
        rec["status"] = status
        rec["ttfb"] = int((time.time() - rec["t0"]) * 1000)

    def chunk(self, rec, chunk):
        if len(rec["raw"]) < MAX_RAW:
            rec["raw"].extend(chunk)

    def finish(self, rec):
        rec["latency"] = int((time.time() - rec["t0"]) * 1000)
        rec["done"] = True

    def fail(self, rec, err):
        rec["conn_error"] = str(err)
        self.finish(rec)

    def clear(self):
        with self.lock:
            self.recs = []

    def reply(self, rec) -> dict:
        n = len(rec["raw"])
        if rec["cache"][0] != n or not rec["done"]:
            if rec["cache"][0] != n:
                rec["cache"] = (n, parse_reply(bytes(rec["raw"]), rec["status"]))
        return rec["cache"][1] or parse_reply(b"", rec["status"])

    # ---- view ----
    def view(self) -> dict:
        with self.lock:
            recs = list(self.recs)
        replies = {r["id"]: self.reply(r) for r in recs}
        calls = {}
        for r in recs:
            for t in replies[r["id"]]["tools"]:
                if t.get("id"):
                    calls.setdefault(t["id"], {"rec": r["id"], "name": t.get("name", "")})
        results = {}
        carried = {}  # rec id -> list of (result dict)
        for r in recs:
            for res in r["req"]["results"]:
                if res["id"] and res["id"] not in results:
                    results[res["id"]] = {"rec": r["id"], "error": res["error"], "preview": res["text"][:300]}
                    carried.setdefault(r["id"], []).append(res)
        last_in_thread = {}
        out = []
        for r in recs:
            q, rp = r["req"], replies[r["id"]]
            errors = []
            if r["conn_error"]:
                errors.append("upstream connection failure: " + r["conn_error"])
            elif r["status"] >= 400:
                errors.append("HTTP %d%s" % (r["status"], (": " + rp["error"]) if rp["error"] else ""))
            for res in carried.get(r["id"], []):
                if res["error"]:
                    nm = calls.get(res["id"], {}).get("name", "")
                    errors.append("tool_result is_error: %s%s" % (nm or "tool", " (" + res["id"] + ")"))
            if rp["error"] and r["status"] < 400 and not r["conn_error"]:
                errors.append("stream error event: " + rp["error"])
            # label
            if r["path"].split("?")[0].endswith("/api/hello"):
                label = "connectivity check"
            elif q["json"] and r["method"] == "POST" and not q["has_tools"]:
                label = "side call: no tools"
            elif q["json"] and q["has_tools"]:
                label = "agent call"
            else:
                label = "other request"
            # new since previous (same thread)
            new = None
            if q["json"]:
                prev = last_in_thread.get(q["thread"])
                n = len(q["messages"])
                if prev is None:
                    new = {"first": True, "diverged": False, "count": n, "start": max(n - 1, 0),
                           "sys": False, "tools": False}
                else:
                    p = 0
                    for a, b in zip(prev["msg_hashes"], q["msg_hashes"]):
                        if a != b:
                            break
                        p += 1
                    ext = p == len(prev["msg_hashes"])
                    new = {"first": False, "diverged": not ext, "count": max(n - p, 0) if ext else 0,
                           "start": p if ext else max(n - 1, 0),
                           "sys": prev["sys_hash"] != q["sys_hash"], "tools": prev["tools_hash"] != q["tools_hash"]}
                last_in_thread[q["thread"]] = q
            # tokens / cost
            u = rp["usage"]
            model = rp["model"] or q["model"]
            cost = None
            if any(v is not None for v in u.values()):
                for sub, pi, po in RATES:
                    if sub in model.lower():
                        cost = ((u["in"] or 0) * pi + (u["out"] or 0) * po
                                + (u["cw"] or 0) * pi * 1.25 + (u["cr"] or 0) * pi * 0.1) / 1e6
                        break
            tool_names = []
            for t in rp["tools"]:
                if t.get("name") and t["name"] not in tool_names:
                    tool_names.append(t["name"])
            for res in carried.get(r["id"], []):
                nm = calls.get(res["id"], {}).get("name")
                if nm and nm not in tool_names:
                    tool_names.append(nm)
            rtools = []
            for t in rp["tools"]:
                res = results.get(t.get("id"))
                rtools.append({"id": t.get("id", ""), "name": t.get("name", ""), "input": t.get("input", ""),
                               "result": res})
            out.append({
                "id": r["id"], "ts": r["t0"], "clock": time.strftime("%H:%M:%S", time.localtime(r["t0"])),
                "method": r["method"], "path": r["path"], "status": r["status"], "done": r["done"],
                "latency": r["latency"], "ttfb": r["ttfb"], "label": label, "model": model, "errors": errors,
                "sizes": q["sizes"], "n_messages": len(q["messages"]), "tokens": u, "cost": cost,
                "new": new, "tool_names": tool_names, "has_detail": q["json"],
                "reply": {"text": rp["text"], "thinking": rp["thinking"], "stop": rp["stop"], "tools": rtools,
                          "error": rp["error"]},
            })
        pairs = {}
        for tid, c in calls.items():
            res = results.get(tid)
            pairs[tid] = {"call": c["rec"], "name": c["name"], "result": res["rec"] if res else None,
                          "error": bool(res and res["error"])}
        ok = sum(1 for p in pairs.values() if p["result"] and not p["error"])
        bad = sum(1 for p in pairs.values() if p["result"] and p["error"])
        return {"records": out, "pairs": pairs, "cost_note": COST_NOTE,
                "summary": {"cards": len(out), "paired_ok": ok, "paired_failed": bad,
                            "error_cards": sum(1 for o in out if o["errors"])}}

    def detail(self, rid: int):
        with self.lock:
            rec = next((r for r in self.recs if r["id"] == rid), None)
        if rec is None:
            return None
        q = rec["req"]
        return {"id": rid, "system": q["system"], "tools": q["tool_defs"],
                "messages": [norm_message(m) for m in q["messages"]]}


STORE = Store()


class Tap(proxy.Tap):
    def relay(self) -> None:  # forwards like proxy.Tap.relay, records quietly
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        rec = None
        try:
            rec = STORE.start(self.command, self.path, body)
        except Exception:
            pass

        def safe(fn, *a):
            if rec is not None:
                try:
                    fn(rec, *a)
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
        except (OSError, http.client.HTTPException) as e:
            safe(STORE.fail, e)
            try:
                self.send_error(502, f"proxy: cannot reach {proxy.UPSTREAM}: {e}")
            except OSError:
                pass
            self.close_connection = True
            return
        safe(STORE.status, reply.status)
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
                    safe(STORE.chunk, chunk)
        except OSError as e:
            safe(STORE.fail, "client/upstream stream broke: %s" % e)
            self.close_connection = True
            return
        finally:
            upstream.close()
        safe(STORE.finish)
        self.close_connection = True


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
        p = self.path.split("?")[0]
        try:
            if p in ("/", "/index.html", "/dashboard.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    self.send(200, "text/html; charset=utf-8", f.read())
            elif p == "/api/records":
                self.send(200, "application/json", json.dumps(STORE.view(), ensure_ascii=False).encode())
            elif p.startswith("/api/req/"):
                d = STORE.detail(int(p.rsplit("/", 1)[1]))
                if d is None:
                    self.send(404, "application/json", b'{"error":"not found"}')
                else:
                    self.send(200, "application/json", json.dumps(d, ensure_ascii=False).encode())
            else:
                self.send(404, "text/plain", b"not found")
        except (ValueError, OSError) as e:
            try:
                self.send(500, "text/plain", str(e).encode())
            except OSError:
                pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        if self.path.split("?")[0] == "/api/clear":
            STORE.clear()
            self.send(200, "application/json", b"{}")
        else:
            self.send(404, "text/plain", b"not found")


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent traffic monitor: proxy + live dashboard.")
    ap.add_argument("--port", type=int, default=8041, help="proxy port (default 8041)")
    ap.add_argument("--ui-port", type=int, default=8050, help="dashboard port (default 8050)")
    ap.add_argument("--upstream", default=proxy.UPSTREAM)
    args = ap.parse_args()
    proxy.UPSTREAM = args.upstream.rstrip("/")
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    ui = ThreadingHTTPServer(("127.0.0.1", args.ui_port), UI)
    ui.daemon_threads = True
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    srv.daemon_threads = True
    print(f"proxy http://localhost:{args.port} -> {proxy.UPSTREAM}\ndashboard http://localhost:{args.ui_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
