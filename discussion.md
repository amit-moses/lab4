# Lab 4 — Discussion notes

(Notes for the discussion; based on the two builds in `spec-history.md`. Builds 1-2 were tested on mock traffic; build 2 was then run on real Claude Code traffic.)

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
(Real run: `claude -p "read note.txt"` through the dashboard, 4 requests.)
- Claude Code makes side calls: a HEAD check and a separate small call that titles the session. They are not part of the agent loop.
- 140 KB of a 173 KB request is tool definitions (37 tools); the system prompt is only 6 KB. All of it is resent every time.
- Caching: 60,309 tokens written in request #3 (est. $0.227), read back in #4 (est. $0.019), about 12x cheaper.
- The prompt is wrapped in injected `<system-reminder>` blocks, and the messages include a `role: "system"` entry.
- The tool call and its result are one request apart, as expected, and the dashboard paired them.
- A real flaw it exposed in our build: the diff compared #3 to the title call (#2), so it reported "system changed, tools changed". The diff must follow the same thread; this is in the spec now but not rebuilt.

## When would you stop regenerating and start editing the code?
When the build is mostly right and the remaining problems are local (a rendering bug, one wrong rule), editing is cheaper and cannot break what already works. Keep regenerating while the problems come from the spec being ambiguous, since a fix in the code does not fix the spec. Here, after build 2 passed the scripted check, I would switch to editing, and keep the spec as the record of decisions.
