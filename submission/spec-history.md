# Spec history — Lab 4 (Agent Traffic Monitor)

Source: `git log -p SPEC.md` (6 commits). Times are the real commit times (18:25, 18:42, 18:49, 19:08, 20:11, 20:15).

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

## Commits 5 and 6 — `aa861f4` (20:11), `5500c43` (20:15) Save PDF button
- **Changed:** added an acceptance criterion: a "Save PDF" button exports every shown request through the print dialog, and (commit 6) the export must contain ALL data, including collapsed or truncated text and the raw request/response JSON, with polling paused while printing.
- **Why:** the hand-in should be a PDF of the traffic. The button already existed in `final-build/` (added directly in the code, see below); these commits put the requirement into the spec so that a rebuild from the spec produces it.
- **Builds from these commits:** build 3 (from commit 5) and build 4 (from commit 6) were started from scratch in empty folders; their results are in the section "Builds 3 and 4" below.

## Builds 3 and 4 (from scratch, from commits 5 and 6) → what they showed
- Both passed the scripted 4-request session (4 cards, 1 paired success, 1 failed pairing, error flags on #3 and #4; fake API key never in the output) and both labelled the HEAD ping and the no-tools title call as side calls, with a thread-aware "new since previous". I re-ran both checks myself.
- **PDF content differed, and the spec wording pinned it down.** I printed each dashboard to PDF in headless Edge (normal browser print path) and searched the text. Build 3 (built from the weaker commit-5 wording): 9 pages; full system prompt, tool definitions and tool results present, but **no raw request/response JSON** (`tool_use_id` appears 0 times; there is no raw view in the code). Build 4 (commit 6 explicitly demands raw JSON and untruncated text): 20 pages; raw JSON present (`tool_use_id` 5 times), full system prompt, tools, results and the 429 body. The API key is absent in both.
- Not verified for either build: clicking the Save PDF button in a real print dialog (only the print path itself was tested headless), dark theme, long live streams.
- **The final build is build 4**, copied to `final-build/` (it replaces the earlier build-2 copy, which has the hand-added button; that version remains in git history).

## Switch to editing
The builds themselves were from scratch. After build 2 (and before builds 3 and 4), a **Save PDF** button (prints every shown request fully expanded) was added to `final-build/dashboard.html` by hand-editing the code, made on another computer (commit `8307a4c`, merged from branch `dashboard-save-pdf`). At that time it was not in the spec; the reason is to hand in the traffic as a PDF. Commits 5 and 6 later put it into the spec, and builds 3 and 4 were generated from the spec, so the final build contains it without hand editing.

## Findings in the traffic
Real Claude Code traffic (one `-p` run, 4 requests; model `claude-sonnet-5-5`):
1. **Not one conversation.** Before the real work Claude Code sent `HEAD /api/hello` (a connectivity check) and a separate small call (about 4 KB, no tools, its own system prompt) that asked the model to title the session; the reply was `{"title":"Contents of note.txt"}`. Only requests #3 and #4 were the agent loop.
2. **The tools dominate the payload.** The main request had 37 tool definitions: about 140 KB of a 173 KB request (81%), against only 6 KB of system prompt. The same block is resent in every request.
3. **Prompt caching decides the cost.** Request #3 wrote 60,309 tokens to the cache (estimated $0.227); request #4 read the same 60,309 tokens back (estimated $0.019), about 12x cheaper. Only 2 uncached input tokens were billed in each. The system blocks carry `cache_control` with a 1-hour TTL. (Costs use the dashboard's hard-coded rates, so they are estimates.)
4. **The user prompt is wrapped.** The first user message holds two injected `<system-reminder>` blocks (account context and commit attribution rules) before the actual prompt, and the messages array contains a `role: "system"` entry with environment info.
5. **Tool pairing in the wild.** The `Read` call in reply #3 came back as a `tool_result` in request #4 (`1	hello from lab 4`, line-numbered), a one-request delay, as the spec assumed. #4 carried the whole history again plus the result.
6. **Request settings worth seeing:** `max_tokens` 128000, `thinking: adaptive` (with display omitted) and a `context_management` field appear on the main calls but not on the title call.

Longer real session (18 requests, 11 minutes, `claude-opus-5-5`, exported to `monitor1.pdf` by the earlier version of the dashboard; I only have the PDF's collapsed headers, not the raw payloads):
7. **One HEAD `/api/hello` before each user turn**: 6 pings (#1, #3, #6, #9, #13, #17) for 6 prompts, each about 0.7-1.5 s.
8. **One request dominated the cost.** The total estimate was $2.634 (the 6 HEAD pings count as "n/a"). Request #14 wrote 54,604 tokens to the cache and cost $1.064, about 40% of the whole session, versus roughly $0.07-0.17 for the ordinary requests. It also carried "+58 msgs", so the history jumped by 58 messages at once. Cache reads on normal follow-ups were 10-55K tokens.
9. **Fresh input is almost always 2 tokens**; everything else is cache read or cache write, so cost is driven by cache behaviour, not by the prompt.
10. **Tool failures were frequent:** 4 error flags in 18 requests (a failed Read, and three Bash errors, two of them in one request, #14). The model recovered each time and the dashboard showed `Bash: failed` / `Read: failed` pairings.
11. **The agent adapted to a missing tool:** replies #10 and #12 say it had no `Glob` tool in that session and used Bash `find` instead; the paths (`/root/lab4/...`) show it ran on Linux.
12. Caveat: the "system changed, tools changed" badges in that PDF came from the earlier diff that compared against the previous request, so they are unreliable (see finding 5 above).
13. The same PDF contains only collapsed card headers, none of the system prompt, tools or messages, which is why the spec now requires the export to contain all the data.

Mock-traffic observations that still hold: failed tools return HTTP 200 (only the content shows the error); HTTP errors such as 429 carry no usage numbers, so cost must be "n/a".
