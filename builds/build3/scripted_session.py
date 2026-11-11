#!/usr/bin/env python3
"""Scripted check: fake upstream + dashboard on spare ports, 4 requests, assertions.

    python scripted_session.py [--upstream-port 18040] [--port 18041] [--ui-port 18050] [--hold SECONDS]

--hold N keeps the dashboard up N seconds after the asserts (and sends two extra side
calls first) so a browser can be pointed at http://localhost:<ui-port>.
Stops only the processes/servers it started.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = "sk-ant-SECRET-do-not-leak"


def sse(*events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def reply_for(kind: str, tool: tuple | None, text: str) -> bytes:
    ev = [
        {"type": "message_start", "message": {"id": "msg_1", "model": "claude-sonnet-4-5", "role": "assistant",
         "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 300}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
    ]
    if tool:
        tid, name, inp = tool
        s = json.dumps(inp)
        ev += [{"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}},
               {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": s[:5]}},
               {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": s[5:]}},
               {"type": "content_block_stop", "index": 1}]
    ev += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"}, "usage": {"output_tokens": 77}},
           {"type": "message_stop"}]
    return sse(*ev)


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n)
        if b"trigger-429" in body:
            out = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        d = json.loads(body)
        if not d.get("tools"):
            out = reply_for("side", None, "Fix login bug")
        else:
            n_msgs = len(d["messages"])
            out = {1: reply_for("m", ("toolu_1", "Read", {"file_path": "/a.txt"}), "I will read the file."),
                   3: reply_for("m", ("toolu_2", "Bash", {"command": "false"}), "Now run a command."),
                   5: reply_for("m", None, "The command failed; giving up.")}.get(n_msgs, reply_for("m", None, "ok"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        mid = len(out) // 2
        self.wfile.write(out[:mid])
        self.wfile.flush()
        time.sleep(0.05)
        self.wfile.write(out[mid:])


def call(port, method, path, obj=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    body = json.dumps(obj).encode() if obj is not None else None
    c.request(method, path, body=body, headers={"content-type": "application/json", "x-api-key": SECRET,
                                                 "authorization": "Bearer " + SECRET})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream-port", type=int, default=18040)
    ap.add_argument("--port", type=int, default=18041)
    ap.add_argument("--ui-port", type=int, default=18050)
    ap.add_argument("--hold", type=float, default=0)
    a = ap.parse_args()

    up = ThreadingHTTPServer(("127.0.0.1", a.upstream_port), Fake)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    dash = subprocess.Popen([sys.executable, os.path.join(HERE, "dashboard.py"), "--port", str(a.port),
                             "--ui-port", str(a.ui_port), "--upstream", f"http://127.0.0.1:{a.upstream_port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                call(a.ui_port, "GET", "/api/records")
                break
            except OSError:
                time.sleep(0.1)
        tools = [{"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}},
                 {"name": "Bash", "description": "Run a shell command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}]
        base = {"model": "claude-sonnet-4-5", "stream": True, "system": [{"type": "text", "text": "You are a coding agent. " * 40}],
                "tools": tools}
        u1 = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>be brief</system-reminder>"},
                                          {"type": "text", "text": "Read /a.txt please"}]}
        a1 = {"role": "assistant", "content": [{"type": "text", "text": "I will read the file."},
                                                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/a.txt"}}]}
        r1 = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file contents"}]}
        a2 = {"role": "assistant", "content": [{"type": "text", "text": "Now run a command."},
                                                {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "false"}}]}
        r2 = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True, "content": "exit code 1"}]}
        a3 = {"role": "assistant", "content": [{"type": "text", "text": "The command failed; giving up."}]}
        u4 = {"role": "user", "content": "trigger-429 please continue"}
        steps = [[u1], [u1, a1, r1], [u1, a1, r1, a2, r2], [u1, a1, r1, a2, r2, a3, u4]]
        statuses = []
        for msgs in steps:
            st, _ = call(a.port, "POST", "/v1/messages", dict(base, messages=msgs))
            statuses.append(st)
        time.sleep(0.3)
        st, raw = call(a.ui_port, "GET", "/api/records")
        d = json.loads(raw)
        s = d["summary"]
        print("statuses", statuses, "summary", s)
        assert statuses == [200, 200, 200, 429], statuses
        assert s["cards"] == 4, s
        assert s["paired_ok"] == 1, s
        assert s["paired_failed"] == 1, s
        assert s["error_cards"] == 2, s
        flagged = sorted(r["id"] for r in d["records"] if r["errors"])
        assert flagged == [3, 4], flagged
        r1c = d["records"][0]
        assert r1c["tokens"]["in"] == 1200 and r1c["tokens"]["cr"] == 800 and r1c["tokens"]["cw"] == 300 and r1c["tokens"]["out"] == 77
        assert r1c["cost"] is not None and d["records"][1]["new"]["count"] == 2, d["records"][1]["new"]
        assert json.loads(d["records"][0]["reply"]["tools"][0]["input"]) == {"file_path": "/a.txt"}
        # secrets must never reach the browser side
        for p in ("/api/records", "/api/req/1", "/api/req/3", "/"):
            assert SECRET.encode() not in call(a.ui_port, "GET", p)[1], p
        det = json.loads(call(a.ui_port, "GET", "/api/req/2")[1])
        assert [m["role"] for m in det["messages"]] == ["user", "assistant", "user"]
        print("OK: 4 cards, 1 paired success, 1 failed pairing, 2 error cards (#3, #4)")
        if a.hold:
            print("extras: HEAD hello + title side call; holding", a.hold, "s")
            call(a.port, "HEAD", "/api/hello")
            call(a.port, "POST", "/v1/messages", {"model": "claude-haiku-4-5", "stream": True, "system": "Title the chat",
                                                   "messages": [{"role": "user", "content": "hi"}]})
            time.sleep(a.hold)
        return 0
    finally:
        dash.terminate()
        try:
            dash.wait(5)
        except subprocess.TimeoutExpired:
            dash.kill()
        up.shutdown()
        up.server_close()


if __name__ == "__main__":
    sys.exit(main())
