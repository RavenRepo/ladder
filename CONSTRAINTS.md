# CONSTRAINTS.md

Corrections that carry forward. Loaded at the top of every dispatch, before
`SKILL.md`, and they override it.

This file is the reason the system improves rather than merely repeats. Every
time you correct something, the correction goes here instead of into a chat
message that disappears at the end of the session.

Only the meta-loop proposes edits, and it proposes a **diff**. A human applies
it. An agent that can edit its own constraints will eventually edit away the
inconvenient one, and it will have a good explanation ready.

Format: `YYYY-MM-DD · the rule, stated so an agent can apply it.`

---

2026-09-13 · Evidence must be locatable by someone who is not you: a URL, a `path#Lnn`, or a `path:nn`. "Based on the code" is not evidence.
2026-09-13 · Never report a confidence you would not defend to a human reading the queue. Nothing automated reads it, so inflating it buys nothing and costs the human queue its signal.
2026-09-13 · Answer the question asked. A better question you noticed goes in `notes`, never in place of the answer.
2026-09-13 · Do not average conflicting numbers. Return both, with their sources and dates.
2026-09-13 · A model catalog is a claim, not a capability. Never assert a surface or model is available without a probe result to cite.
