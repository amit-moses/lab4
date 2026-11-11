# Discussion

## Show your latest build running on live traffic
Run `python dashboard.py` in `final-build/`, open http://localhost:8050, and start Claude Code with `ANTHROPIC_BASE_URL=http://localhost:8041`. Requests appear on the page while the agent works.

## Which decisions did you only discover by building?
What counts as an "error", and how the dashboard connects to `proxy.py`. Build 1 flagged a request as an error because its tool call would fail later, and it replaced `proxy.py`. I only saw this in its output, so I put both rules into the spec.

## What differed between builds, and what in the spec pinned it down?
Build 1 flagged the wrong request and showed broken text (`[object HTMLSpanElement]`); build 2 did not, because the spec then defined errors and rendering. Build 3 saved a PDF without the raw JSON; build 4 included it, because the spec said "all the data, including raw JSON".

## What interesting things did you find in the agent's messages?
Tool definitions are about 140 KB of a 173 KB request and are resent every time. Cache decides the cost: in one session a single request wrote 54,604 tokens to the cache and cost $1.06, about 40% of the total $2.63. Claude Code also sends a HEAD check before each turn and a small request that names the session.

## When would you stop regenerating and start editing the code?
When the build is almost right and only small local things are left, like a button. I keep regenerating when the spec itself is unclear, because fixing the code does not fix the spec.
