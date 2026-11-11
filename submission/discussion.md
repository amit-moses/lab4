# Discussion

## Short summary of the results
I built the traffic dashboard four times from the spec, each time from scratch. I tested it on a fake API (a scripted session) and on real Claude Code traffic: a short run of 4 requests, and a longer run of 18 requests that I only have as a PDF. The dashboard showed every request, newest first, paired each tool call with its result, flagged errors, and showed tokens, cost and time. The real traffic showed that Claude Code sends extra requests (a HEAD check before each turn and a small request that names the session), that the tool definitions are most of every request, and that cost depends mostly on the prompt cache. The PDF button saves every shown request as a PDF, with all the hidden text open.

## Show your latest build running on live traffic
Run `python dashboard.py` in `final-build/`, open http://localhost:8050, and start Claude Code with `ANTHROPIC_BASE_URL=http://localhost:8041`. The requests appear on the page while the agent works. This is build 4, made from the spec.

## Which decisions did you only discover by building?
What counts as an "error", and how the dashboard connects to `proxy.py`. Build 1 marked a request as an error because its tool call would fail later, and it replaced `proxy.py` instead of using it. I only saw this in its output, so I wrote both rules into the spec. Real traffic then showed two more: side requests must be labelled, and the "what changed" comparison must follow the same conversation.

## What differed between builds, and what in the spec pinned it down?
Build 1 flagged the wrong request as an error and showed broken text like `[object HTMLSpanElement]`; build 2 did neither, because the spec now defined errors and the rendering rules. The PDF export also differed. Build 3 (weaker wording) saved the full text but not the raw JSON; build 4 saved everything, because the spec said "all the data, including raw request and response JSON". So the exact wording of the spec decided the result.

## What interesting things did you find in the agent's messages?
Every request resends all the tool definitions, about 140 KB of a 173 KB request. A tool result arrives one request after the tool call. Cache decides the cost: in the long session one request wrote 54,604 tokens to the cache and cost $1.06, about 40% of the whole $2.63. There is one HEAD check per turn, and the agent worked around a missing tool by using another one.

## When would you stop regenerating and start editing the code?
When the build is almost right and only small local problems are left, such as a button or one wrong rule, because editing is faster and keeps what already works. I keep regenerating when the problem comes from an unclear spec, because a code fix does not fix the spec. Here the PDF button was first added directly in the code; I then put it into the spec and regenerated, so the spec stays the source of truth.

## About the PDF button
The "Save PDF" button prints every request that is currently shown, with all sections open, long text in full and the raw JSON included; then you choose "Save as PDF". It pauses the live updates while printing and restores the page afterwards. It was first added directly in the code on another computer; later it was added to the spec, and builds 3 and 4 produced it from the spec. I tested the print output in a headless browser, but not a click on the button in a real print dialog.
