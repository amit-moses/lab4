"""Mock Anthropic upstream + scripted "agent" used to test each dashboard build.

    python tools/mock_upstream.py serve 9100        # fake API (SSE replies)
    python tools/mock_upstream.py drive 8041        # send a 3-turn session via the proxy

Turn 1: model answers with text + tool_use (Read). Turn 2: request carries the tool_result,
model answers with a Bash tool_use. Turn 3: tool_result is_error=true, model apologises. Turn 4: HTTP 429.
Not real Claude Code traffic - it only mimics the documented wire format.
"""
import json, sys, time, http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SYSTEM = [{"type": "text", "text": "You are Claude Code, Anthropic's CLI.\n" + "Follow the rules carefully. " * 120}]
TOOLS = [
    {"name": "Read", "description": "Reads a file", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}},
    {"name": "Bash", "description": "Runs a shell command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
]


def sse(events):
    out = b""
    for name, data in events:
        out += f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
    return out


def reply(n):
    msg = {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
           "usage": {"input_tokens": 1200 * n, "output_tokens": 1, "cache_read_input_tokens": 900}}
    ev = [("message_start", {"type": "message_start", "message": msg})]
    if n == 1:
        ev += [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "I'll read "}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "the file."}}),
               ("content_block_stop", {"type": "content_block_stop", "index": 0}),
               ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {}}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"file_path\": \"/tmp/"}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "a.txt\"}"}}),
               ("content_block_stop", {"type": "content_block_stop", "index": 1})]
        stop = "tool_use"
    elif n == 2:
        ev += [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {}}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"command\": \"cat /nope\"}"}}),
               ("content_block_stop", {"type": "content_block_stop", "index": 0})]
        stop = "tool_use"
    else:
        ev += [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "That command failed."}}),
               ("content_block_stop", {"type": "content_block_stop", "index": 0})]
        stop = "end_turn"
    ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 40 * n}}),
           ("message_stop", {"type": "message_stop"})]
    return sse(ev)


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    count = 0

    def log_message(self, *a): pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        Fake.count += 1
        n = Fake.count
        time.sleep(0.4)
        if n == 4:
            data = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode()
            self.send_response(429); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data))); self.send_header("Connection", "close"); self.end_headers()
            self.wfile.write(data); self.close_connection = True; return
        data = reply(n)
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
        for i in range(0, len(data), 200):
            self.wfile.write(data[i:i + 200]); self.wfile.flush(); time.sleep(0.05)
        self.close_connection = True


def post(port, payload):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", "/v1/messages", json.dumps(payload), {"content-type": "application/json", "x-api-key": "sk-secret", "anthropic-version": "2023-06-01"})
    r = c.getresponse(); r.read(); return r.status


def drive(port):
    base = {"model": "claude-sonnet-5-5", "max_tokens": 4096, "stream": True, "system": SYSTEM, "tools": TOOLS}
    m = [{"role": "user", "content": "Please read /tmp/a.txt"}]
    print(post(port, {**base, "messages": m}))
    m += [{"role": "assistant", "content": [{"type": "text", "text": "I'll read the file."}, {"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {"file_path": "/tmp/a.txt"}}]},
          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": "hello world"}]}]
    print(post(port, {**base, "messages": m}))
    m += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {"command": "cat /nope"}}]},
          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_B", "is_error": True, "content": "cat: /nope: No such file"}]}]
    print(post(port, {**base, "messages": m}))
    print(post(port, {**base, "messages": m + [{"role": "user", "content": "try again"}]}))


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Fake).serve_forever()
    else:
        drive(int(sys.argv[2]))
