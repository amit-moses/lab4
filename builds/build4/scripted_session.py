#!/usr/bin/env python3
"""scripted_session.py: runnable acceptance check (no real agent needed).

Starts a fake SSE upstream and dashboard.py on spare ports, sends a 4-request
session (text + tool call, tool result, failed tool result, HTTP 429), asserts
the acceptance results, then stops what it started.

    python scripted_session.py [--proxy-port 19041] [--ui-port 19050] [--upstream-port 19100] [--no-browser]
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
TOOLS = [{"name": "Bash", "description": "Run a shell command. " + "x" * 600,
          "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
         {"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}}]
SYSTEM = "You are a coding agent. " + "SYSTEM-MARKER-LONG " * 60


def sse(*events):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in events).encode()


def reply(text, tool=None, tid=None, cmd=None):
    ev = [("message_start", {"type": "message_start", "message": {"model": "claude-sonnet-4-5", "usage": {
        "input_tokens": 120, "output_tokens": 1, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 300}}}),
          ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
          ("content_block_stop", {"type": "content_block_stop", "index": 0})]
    if tool:
        ev += [("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": tid, "name": tool, "input": {}}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"command": '}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": json.dumps(cmd) + "}"}}),
               ("content_block_stop", {"type": "content_block_stop", "index": 1})]
    ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"}, "usage": {"output_tokens": 42}}),
           ("message_stop", {"type": "message_stop"})]
    return ev


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    n = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        Fake.n += 1
        n = Fake.n
        if n == 4:
            data = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        evs = {1: reply("I will list the files.", "Bash", "toolu_A", "ls -la"),
               2: reply("Now reading.", "Bash", "toolu_B", "cat missing.txt"),
               3: reply("That failed; giving up. " + "long " * 150)}[n]
        raw = sse(*evs)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for i in range(0, len(raw), 200):
            self.wfile.write(raw[i:i + 200])
            self.wfile.flush()
            time.sleep(0.01)
        self.close_connection = True


def post(port, payload):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", "/v1/messages", json.dumps(payload), {"content-type": "application/json", "x-api-key": "sk-secret-TEST"})
    r = c.getresponse()
    data = r.read()
    return r.status, data


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", path)
    return c.getresponse().read()


def wait_port(port, secs=15):
    end = time.time() + secs
    while time.time() < end:
        try:
            http.client.HTTPConnection("127.0.0.1", port, timeout=1).connect()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"port {port} did not open")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-port", type=int, default=19041)
    ap.add_argument("--ui-port", type=int, default=19050)
    ap.add_argument("--upstream-port", type=int, default=19100)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--hold", type=int, default=0, help="keep servers up this many seconds after the checks (manual inspection)")
    a = ap.parse_args()

    fake = ThreadingHTTPServer(("127.0.0.1", a.upstream_port), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    dash = subprocess.Popen([sys.executable, os.path.join(HERE, "dashboard.py"), "--port", str(a.proxy_port),
                             "--ui-port", str(a.ui_port), "--upstream", f"http://127.0.0.1:{a.upstream_port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        wait_port(a.proxy_port)
        wait_port(a.ui_port)
        base = {"model": "claude-sonnet-4-5", "system": SYSTEM, "tools": TOOLS, "max_tokens": 1000, "stream": True}
        u1 = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>be brief</system-reminder>"}, {"type": "text", "text": "list the files"}]}
        a1 = {"role": "assistant", "content": [{"type": "text", "text": "I will list the files."},
                                               {"type": "tool_use", "id": "toolu_A", "name": "Bash", "input": {"command": "ls -la"}}]}
        r1 = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": "file1\nfile2 " + "r" * 500}]}
        a2 = {"role": "assistant", "content": [{"type": "text", "text": "Now reading."},
                                               {"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {"command": "cat missing.txt"}}]}
        r2 = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_B", "is_error": True, "content": "cat: missing.txt: No such file"}]}
        sts = [post(a.proxy_port, {**base, "messages": m})[0] for m in
               ([u1], [u1, a1, r1], [u1, a1, r1, a2, r2], [u1, a1, r1, a2, r2, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "again"}])]
        assert sts == [200, 200, 200, 429], sts
        time.sleep(0.3)
        data = json.loads(get(a.ui_port, "/api/records?since=0&epoch=0"))
        recs = data["records"]
        s = data["summary"]
        assert len(recs) == 4, f"cards {len(recs)}"
        assert s["paired_ok"] == 1 and s["failed"] == 1, s
        flagged = [r["d"]["id"] for r in recs if r["d"]["errors"]]
        assert flagged == [3, 4], flagged  # carrier of the failed result, and the 429
        assert recs[1]["d"]["new"] == {"mode": "ext", "count": 2, "total": 3, "system_changed": False, "tools_changed": False, "prev": 1}, recs[1]["d"]["new"]
        assert recs[0]["d"]["usage"]["cache_read"] == 5000 and recs[0]["d"]["cost"] is not None
        assert "sk-secret-TEST" not in json.dumps(data), "secret leaked"
        print("API checks passed:", s, "flagged cards:", flagged)

        if not a.no_browser and os.path.exists(EDGE):
            tmp = tempfile.mkdtemp()
            try:
                def dump(q):
                    out = subprocess.run([EDGE, "--headless=new", "--disable-gpu", f"--user-data-dir={tmp}", "--virtual-time-budget=6000",
                                          "--dump-dom", f"http://127.0.0.1:{a.ui_port}/{q}"], capture_output=True, timeout=90).stdout.decode("utf-8", "replace")
                    return out
                dom = dump("")
                assert len(re.findall(r'class="card', dom)) == 4, "browser: expected 4 cards"
                body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", dom.split("<body", 1)[1], flags=re.S)
                body = re.sub(r"<[^>]+>", " ", body)
                for bad in ("[object", "undefined", "NaN"):
                    assert bad not in body, f"artifact {bad!r} in page"
                pdom = dump("?print=1")
                for must in ("SYSTEM-MARKER-LONG", "cat: missing.txt", "ls -la", "rate_limit_error", "cache_creation_input_tokens"):
                    assert must in pdom, f"print mode lacks {must}"
                assert "r" * 500 in pdom
                print("browser checks passed (4 cards, no artifacts, print mode contains hidden data)")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("browser checks skipped")
        ok = True
        if a.hold:
            print(f"holding {a.hold}s: UI http://127.0.0.1:{a.ui_port}", flush=True)
            time.sleep(a.hold)
    finally:
        dash.terminate()
        try:
            dash.wait(5)
        except subprocess.TimeoutExpired:
            dash.kill()
        fake.shutdown()
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
