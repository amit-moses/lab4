# Spec history — Lab 4 (Agent Traffic Monitor)

Source: `git log -p SPEC.md` (4 commits). Times are the real commit times (18:25, 18:42, 18:49, 19:08).

**Update:** after the two builds I also ran build 2 on real Claude Code traffic (claude.exe in `-p` mode, prompt "read note.txt", routed through the dashboard on port 8041; 4 requests). That is commit 4 and the real findings below.

**How this session was run (stated plainly):** the agent did the build and test work, so the loop took about 25 minutes of wall-clock time (18:25–18:49), well under the 70-minute lab slot. It is two from-scratch builds, not a longer series. The builds were done by fresh agent sessions in empty folders containing only `SPEC.md` and `proxy.py`. Builds 1 and 2 were tested on a scripted mock upstream (`tools/mock_upstream.py`) that imitates the Anthropic streaming format; real Claude Code traffic was captured afterwards (see below).

## Commit 1 — `18c7880` (18:25) Initial SPEC.md
- **Changed:** the spec prepared before the lab (goal, behavior, constraints, design decisions, acceptance criteria, two open decisions: WebSocket vs polling, how to show diffs).
- **Why:** the pre-lab quiz draft, committed unchanged as the starting point.

## Build 1 (18:25–18:41, from scratch) → what it showed
- Built `dashboard.py` + `dashboard.html`. It worked on the mock traffic (API output correct) but the agent never rendered the page.
- The spec left the architecture open, so build 1 decided it itself: it made `dashboard.py` *replace* `proxy.py` as the proxy.
- Reading the API output: request #2 (HTTP 200) was flagged as an error just because its tool call would fail later. The spec never defined "error".
- Opening the page in headless Edge showed what a JS syntax check missed: `[object HTMLSpanElement]`, `false` and `null` printed in card headers, and a "+-5 msg" badge when the conversation restarted.

## Commit 2 — `9bb154c` (18:42) Pin architecture, errors, pairing, secrets
- **Changed:** added *Architecture* (one command, imports `proxy.py` without editing it, ports 8041/8050, polling, in-memory), *Definition of error* (flag on the request that carries the failed result, HTTP >= 400, stream error), *Tool pairing* by id, a no-secrets rule, and a testable acceptance criterion (a 4-request scripted session with exact expected counts). Replaced the open decisions that builds had settled.
- **Why:** build 1 had to guess the architecture and the error meaning; the spec now decides both and can be checked by script.

## Build 2 (18:42–18:48, from scratch) → what it showed
- Followed the spec: flags only on #3 (carrier of the failed result) and #4 (HTTP 429), "Bash: failed" / "Read: ok" pairing badges, correct tokens/cost, "n/a" cost for unknown model, "first request" instead of a diff for #1. Wrote its own `scripted_session.py`, which passed 8/8; I re-ran it. Screenshot was clean.
- Not verified: expand/collapse and click-through links (only checked through the API and a screenshot of the collapsed list).

## Commit 3 — `3be74c2` (18:49) Rendering and robustness rules
- **Changed:** no raw artifacts (`[object ...]`, `null`, `false`) in the UI; diff counts never negative, show "history diverged" otherwise; first-carrier rule for error flags; `scripted_session.py` and a headless-browser check become required deliverables.
- **Why:** these were build 1's defects, and build 2 handled them by luck of its own choices rather than because the spec required it.
- **Not rebuilt** after this commit.

## Real-traffic run of build 2 → what it showed
- The dashboard handled real traffic: 4 requests, tool call and result paired (`Read: ok`), cache and cost numbers filled in, no errors, no raw artifacts. Screenshot: `builds/shots/real_claude_code.png`.
- Weakness found: request #3 shows "system changed, tools changed" only because the unrelated title call (#2) came just before it; the diff compares with the previous request, not with the previous request of the same thread. HEAD and the title call look like ordinary requests.

## Commit 4 — real-traffic rules (see SPEC.md)
- **Changed:** side calls must be labelled and excluded from the diff baseline; per-request size breakdown (system/tools/messages); cache write/read and cost-estimate caveat; `role: "system"` and `<system-reminder>` blocks shown as their own kind.
- **Why:** the real run above. **Not rebuilt** after this commit.

## Switch to editing
No switch: both builds were from scratch. The final build is build 2, unedited (copied to `final-build/`).

## Findings in the traffic
Real Claude Code traffic (one `-p` run, 4 requests; model `claude-sonnet-5-5`):
1. **Not one conversation.** Before the real work Claude Code sent `HEAD /api/hello` (a connectivity check) and a separate small call (about 4 KB, no tools, its own system prompt) that asked the model to title the session; the reply was `{"title":"Contents of note.txt"}`. Only requests #3 and #4 were the agent loop.
2. **The tools dominate the payload.** The main request had 37 tool definitions: about 140 KB of a 173 KB request (81%), against only 6 KB of system prompt. The same block is resent in every request.
3. **Prompt caching decides the cost.** Request #3 wrote 60,309 tokens to the cache (estimated $0.227); request #4 read the same 60,309 tokens back (estimated $0.019), about 12x cheaper. Only 2 uncached input tokens were billed in each. The system blocks carry `cache_control` with a 1-hour TTL. (Costs use the dashboard's hard-coded rates, so they are estimates.)
4. **The user prompt is wrapped.** The first user message holds two injected `<system-reminder>` blocks (account context and commit attribution rules) before the actual prompt, and the messages array contains a `role: "system"` entry with environment info.
5. **Tool pairing in the wild.** The `Read` call in reply #3 came back as a `tool_result` in request #4 (`1	hello from lab 4`, line-numbered), a one-request delay, as the spec assumed. #4 carried the whole history again plus the result.
6. **Request settings worth seeing:** `max_tokens` 128000, `thinking: adaptive` (with display omitted) and a `context_management` field appear on the main calls but not on the title call.

Mock-traffic observations that still hold: failed tools return HTTP 200 (only the content shows the error); HTTP errors such as 429 carry no usage numbers, so cost must be "n/a".
