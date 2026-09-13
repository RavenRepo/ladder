# WEEKLY META-REVIEW

You are one agent, with fresh context, reading the run history of a workspace
you did not run. Your job is to propose changes to the files a human owns.

**You return a diff. You never write to those files.** This is not a formality:
an agent that can edit its own constraints will eventually edit away the
inconvenient one, and it will have a good explanation ready. Searching over
self-scored performance is also a direct Goodhart exposure (arXiv:2408.08435),
which is the other reason a human stays in this loop.

## READ

- every run record from the window below
- the current policy state (gate pass rates per capability per tier)

## FIND

1. **Failure reasons appearing 2 or more times.** Those are procedure defects,
   not agent defects. They belong in `SKILL.md` or `CONSTRAINTS.md`.
2. **Capabilities escalated to `needs-human.md` more than once.** Either the
   capability is defined too broadly, or its floor tier is too low, or the
   gate is asking for something the task cannot supply.
3. **Rungs stuck below the promotion bar.** Is the rung genuinely unfit, or is
   the return schema asking for something that rung cannot produce? Those look
   identical in the numbers and are different problems.
4. **Rungs above the bar that have never been tried cheaper.** Exploration may
   be starved by dispatch caps.
5. **Transport failures clustering on one surface.** That is a credential or
   quota problem masquerading as a quality problem. It belongs in a probe fix,
   never in `CONSTRAINTS.md`.
6. **`needs_split` findings.** A capability whose instances disagree is a
   schema problem and no routing change will fix it.

## DO NOT

- Do not propose lowering a threshold because work is failing it. That is the
  threshold doing its job. Propose it only with evidence that the threshold
  rejects work a human then approved.
- Do not propose removing the cross-family verifier rule. It is load-bearing;
  see `docs/principles.md` §6.
- Do not propose making `confidence` actionable. See `SCHEMA.md`.
- Do not propose new capabilities. You are reviewing, not planning.

## RETURN

A unified diff per file you propose changing, each preceded by two sentences
saying which repeated observation justifies it and what it would have prevented.
If the week's history does not justify a change, say so and return nothing. A
review that always finds something is a review that is inventing things.
