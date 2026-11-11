# Spec history — Lab 4 (Agent Traffic Monitor)

Source: `git log -p SPEC.md` (3 commits). Times are the real commit times.

**How this session was run (stated plainly):** the agent did the build and test work, so the loop took about 25 minutes of wall-clock time (18:25–18:49), well under the 70-minute lab slot. It is two from-scratch builds, not a longer series. The builds were done by fresh agent sessions in empty folders containing only `SPEC.md` and `proxy.py`. No real Claude Code traffic was captured in this session (no `claude` CLI on this machine); all test traffic came from a scripted mock upstream (`tools/mock_upstream.py`) that imitates the Anthropic streaming format.

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

## Switch to editing
No switch: both builds were from scratch. The final build is build 2, unedited (copied to `final-build/`).

## Findings in the traffic
Observed in the mock traffic only (not real Claude Code traffic; real findings must be added after running a real agent):
- A tool result arrives one request *after* the tool call, so a pairing has to be rebuilt from later requests. Every later request repeats the whole history, so naive error flagging marks the same failure again and again.
- Each request resends the full system prompt and tool list; most of the payload is repeated, which is why "what is new" matters more than raw size.
- A failing tool (`is_error`) still returns HTTP 200; only the content shows the problem. HTTP-level errors (429) look different and carry no usage numbers, so cost must be "n/a", not 0.
- Streamed tool input arrives as partial JSON fragments and has to be assembled before it can be shown.
