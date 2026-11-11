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

## Open Decisions
- Should live updates use WebSockets or simple HTTP polling from `proxy.py`?
- How best to highlight request-to-request diffs (showing what changed since the previous request)?