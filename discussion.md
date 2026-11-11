# Lab 4 — Discussion notes

(Notes for the discussion; based on the two builds in `spec-history.md`. Traffic was mock traffic, not real Claude Code traffic.)

## Show your latest build on live traffic
Run `python dashboard.py` in `final-build/`, open http://localhost:8050, then start the agent with `ANTHROPIC_BASE_URL=http://localhost:8041`. Without an agent: `python scripted_session.py` and `python mock_upstream.py` (see the file header).

## Which decisions did you only discover by building?
- **What an "error" is.** The spec said "filter by error status", but build 1 flagged a request as an error because its tool call would fail later, while the request that actually carried the failed result was not flagged. Only the output made the ambiguity visible.
- **Architecture.** "Connects to proxy.py" hid a real choice: replace it, wrap it, or parse its printout. Build 1 replaced it.
- **Conversation restarts.** The "what changed" counter went negative (+-5 msg) when the history restarted. The spec had not said what a diff means when history is not an extension of the last request.

## What differed between builds, and what in the spec pinned it down?
- Architecture and error flagging differed (build 1 flagged #2, build 2 flagged #3). The new Architecture and Error sections, and the 4-request scripted check with exact expected counts, pinned both. Build 2 followed them and passed.
- Rendering quality differed (build 1 printed `[object HTMLSpanElement]`; build 2 was clean) and the spec did *not* pin it down; commit 3 adds it as a rule, so that is an untested change.
- Caveat: only two builds, so one comparison is weak evidence.

## What interesting things did you find in the agent's messages?
Only in mock traffic: tool results come one request after the call; the full history and system prompt are resent every request; a failed tool still returns HTTP 200; tool inputs stream as JSON fragments. (Anything about real Claude Code's prompts and tools needs a real run; I did not do one.)

## When would you stop regenerating and start editing the code?
When the build is mostly right and the remaining problems are local (a rendering bug, one wrong rule), editing is cheaper and cannot break what already works. Keep regenerating while the problems come from the spec being ambiguous, since a fix in the code does not fix the spec. Here, after build 2 passed the scripted check, I would switch to editing, and keep the spec as the record of decisions.
