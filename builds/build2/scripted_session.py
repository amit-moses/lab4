#!/usr/bin/env python3
"""Acceptance check without a real agent: fake SSE upstream + 4 scripted requests.

    python scripted_session.py     # starts dashboard.py itself, checks results, kills everything
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UP, PROXY, UI = 8199, 8141, 8150


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def reply(text=None, tool=None, model="claude-sonnet-4-5"):
    ev = [{"type": "message_start", "message": {"model": model, "usage": {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 1}}}]
    i = 0
    if text:
        ev += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text[:5]}},
               {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text[5:]}},
               {"type": "content_block_stop", "index": i}]
        i += 1
    if tool:
        ev += [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tool[0], "name": tool[1], "input": {}}},
               {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'}},
               {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}},
               {"type": "content_block_stop", "index": i}]
    ev += [{"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}}, {"type": "message_stop"}]
    return sse(ev)


N = [0]


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        N[0] += 1
        if N[0] == 4:
            b = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode()
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        body = {1: reply("Let me look.", ("t1", "Bash")), 2: reply("Now again.", ("t2", "Read")), 3: reply("It failed.")}[N[0]]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for j in range(0, len(body), 60):
            self.wfile.write(body[j:j + 60]); self.wfile.flush(); time.sleep(0.01)
        self.close_connection = True


def post(msgs):
    req = {"model": "claude-sonnet-4-5", "max_tokens": 100, "system": "You are an agent.",
           "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}], "messages": msgs}
    r = urllib.request.Request(f"http://127.0.0.1:{PROXY}/v1/messages", json.dumps(req).encode(),
                               {"content-type": "application/json", "x-api-key": "SECRET-KEY-123"})
    try:
        return urllib.request.urlopen(r).read()
    except urllib.error.HTTPError as e:
        return e.read()


def main():
    up = ThreadingHTTPServer(("127.0.0.1", UP), Fake)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    dash = subprocess.Popen([sys.executable, "dashboard.py", "--port", str(PROXY), "--ui-port", str(UI),
                             "--upstream", f"http://127.0.0.1:{UP}"], stdout=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        m1 = [{"role": "user", "content": "hi"}]
        a1 = {"role": "assistant", "content": [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}}]}
        m2 = m1 + [a1, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "files"}]}]
        a2 = {"role": "assistant", "content": [{"type": "text", "text": "Now again."}, {"type": "tool_use", "id": "t2", "name": "Read", "input": {"cmd": "ls"}}]}
        m3 = m2 + [a2, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "boom", "is_error": True}]}]
        m4 = m3 + [{"role": "assistant", "content": "It failed."}, {"role": "user", "content": "retry"}]
        for m in (m1, m2, m3, m4):
            post(m)
        time.sleep(1.2)
        s = json.load(urllib.request.urlopen(f"http://127.0.0.1:{UI}/api/summary"))["records"]
        d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{UI}/api/record?id=1"))
        allj = json.dumps(s) + json.dumps(d)
        calls = [c["state"] for r in s for c in r["calls"]]
        errs = [r["id"] for r in s if r["errors"]]
        checks = {
            "4 cards": len(s) == 4,
            "1 paired ok": calls.count("ok") == 1,
            "1 failed pairing": calls.count("failed") == 1,
            "error cards are #3 and #4": errs == [3, 4],
            "no secret leaked": "SECRET-KEY" not in allj,
            "tokens/cost": s[0]["in"] == 100 and s[0]["out"] == 20 and s[0]["cache_read"] == 50 and s[0]["cost"] is not None,
            "new since previous": s[1]["new_count"] == 2 and s[3]["new_count"] == 2,
            "tool args assembled": d["output"][1]["input"] == {"cmd": "ls"},
        }
        for k, v in checks.items():
            print("PASS" if v else "FAIL", k)
        return 0 if all(checks.values()) else 1
    finally:
        dash.kill()
        up.shutdown()


if __name__ == "__main__":
    sys.exit(main())
