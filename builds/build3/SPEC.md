# SPEC.md — Agent Traffic Monitor Dashboard

## Goal and Scope
Build a live browser-based dashboard that intercepts and displays HTTP traffic passing between the agent harness (e.g., Claude Code) and the model API via `proxy.py`.
The objective is to make complex, fast-accumulating message payloads readable and easy to inspect in real time while the agent runs.

Out of scope: Modifying or altering API requests/responses in flight; persistent database storage beyond the current session.

## Behavior
- **Live Traffic Stream:** Show every request sent by the harness and reply received from the model, updated live with the newest items first.
- **Request Inspection & Parsing:** Clearly divide every API request into distinct sections:
  - **System Instructions:** Full system prompt with collapsible view.
  - **Tool Definitions:** List of tools available to the model.
  - **Conversation History:** Messages exchanged so far and the current user prompt.
- **Tool Call & Result Pairing:** Visual association between a model's tool request and its corresponding execution result (which arrives in a subsequent request payload).
- **Model Output Inspection:** Display the assistant's text response alongside requested tool calls.
- **Filtering & Spotting Errors:** Provide tools to filter traffic by tool name or error status to quickly isolate issues.
- **Metrics & Statistics:** Display per-request token usage, estimated costs, and latency without replacing detailed message inspection.

## Integration and Constraints
- Connects directly to the `proxy.py` script listening on a local port (e.g., `localhost:8041`).
- Consumes raw JSON request and response payloads forwarded by the proxy.
- Must provide a lightweight local UI where long text is collapsible and JSON is formatted for readability.

## Design Decisions and Reasons
- **Newest First Ordering:** Ensures that live incoming messages are immediately visible without forcing constant manual scrolling.
- **Collapsible Layout for System & Tools:** System prompts and tool definitions occupy tens of KBs in every request; collapsibility keeps the UI clean while preserving full detail.
- **Paired Tool Calls and Results:** Tool execution results are returned in later requests; linking them explicitly makes the agent's workflow traceable.

## Acceptance Criteria
- [ ] Intercepted requests appear in the dashboard automatically as they occur.
- [ ] Users can expand and collapse system instructions, tool definitions, and JSON objects.
- [ ] Tool calls are visually linked to their corresponding tool result payload.
- [ ] Request components (system, tools, messages) are visually distinct.

## Architecture (decided after build 1)
- Single command: `python dashboard.py` (stdlib only). It imports `proxy.py` (reuse its SKIP list and `--upstream`/`--port` flags; do not edit `proxy.py`), listens on 8041 as the proxy and serves the UI on 8050. `proxy.py` stays runnable on its own.
- The frontend is one `dashboard.html` file, no CDN. Live updates use HTTP polling every 1 s; a streaming reply updates while it is still arriving.
- Forwarding must stay byte-for-byte; recording must never break or delay the agent's call.
- Records are in memory only, with a "Clear" button.
- Security: never store, send to the browser, or display `authorization` / `x-api-key` header values. Headers are not shown at all.

## Definition of "error" (decided after build 1)
An error is flagged **on the request where it is visible**, with the reason shown:
1. HTTP status >= 400 or upstream connection failure (flag on that exchange);
2. a `tool_result` with `is_error: true` (flag on the request that *carries* the result);
3. an `error` event in the streamed reply.
A request is NOT an error just because its tool call will later fail. The paired tool call in the earlier reply is shown as "failed" once the result arrives.

## Tool pairing
Match `tool_use.id` to `tool_result.tool_use_id`. In a reply, each tool call shows its input and, when it arrives, its result (with a link to the request that carries it); before that, "result pending".

## Acceptance Criteria
- [ ] Intercepted requests appear in the dashboard automatically as they occur (within ~2 s), newest first.
- [ ] Users can expand and collapse system instructions, tool definitions, and JSON objects; long text is collapsed by default.
- [ ] Tool calls are visually linked to their corresponding tool result payload.
- [ ] Request components (system, tools, history, new message) are visually distinct.
- [ ] Errors-only and tool-name filters work; filters never hide the detail of a visible request.
- [ ] Each request shows input/output tokens (cache reads separately), latency, estimated cost (unknown model -> "n/a", not 0).
- [ ] A "Save PDF" button prints every currently shown request with all its sections fully expanded (print stylesheet; the user chooses "Save as PDF"); live polling pauses while printing and the previous open/closed state is restored afterwards.
- [ ] Each request shows what is new since the previous request (new messages; system/tools changed).
- [ ] Verifiable without a real agent: a scripted session of 4 requests (text + tool call, tool result, failed tool result, HTTP 429) produces 4 cards, 1 paired success, 1 failed pairing, and 2 error-flagged cards (the carrier of the failed result and the 429).

## Rendering and robustness rules (decided after build 2 review)
- The UI must never print raw programming artifacts: no `[object ...]`, `null`, `undefined`, `false` or `NaN` in the page. (Build 1 showed `[object HTMLSpanElement]` and `false` in every card header; a syntax check did not catch it, only a screenshot did.)
- "New since previous" is computed from the common prefix of messages. It is never negative; if the history is not an extension of the previous request (new session, subagent, compaction) show "history diverged" instead of a count.
- The tool-error flag goes on the first request that carries the result; later requests repeat the full history and are not flagged.
- Deliver a runnable check, `scripted_session.py`, that starts a fake upstream and the dashboard on spare ports, sends the 4-request scripted session and asserts the acceptance results, then stops what it started.
- Verify the page by loading it in a real (headless) browser, not only with a JS syntax check.

## Real-traffic rules (decided after running build 2 on real Claude Code traffic)
- A real session is not one linear conversation. Claude Code also sends side calls: a `HEAD /api/hello` connectivity check and a small no-tools call that generates the session title. These must be labelled (e.g. "side call: no tools" / "connectivity check") and must not be treated as the previous request of the main conversation.
- "New since previous" compares a request with the previous request of the same thread (same tool list / system lineage), not simply the previous request. (Build 2 reported "system changed, tools changed" for the first real main call only because the title side call came before it.)
- Per request, show a size breakdown: bytes of system, tools and messages. (In the real session the tool definitions were about 140 KB of a 173 KB request.)
- Cache metrics are first-class: cache write vs cache read tokens are shown separately, and the cost estimate must note that it is an estimate (cache write price depends on the cache TTL).
- Messages may carry `role: "system"` entries and injected `<system-reminder>` text blocks inside user messages; show them as their own kind, not as plain user text.

## Open Decisions
- Whether to add WebSockets/SSE later (polling is sufficient for now).
- Cost rates are hard-coded estimates; should they be configurable?
