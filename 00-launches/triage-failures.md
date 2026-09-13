---
capability: triage-failures
question: Which recorded gate failures are procedure defects worth a constraint?
lane: thin
floor_tier: sonnet-5
risk_class: reversible
gate: script
optimise: seconds
max_dispatches: 40
max_attempts: 2
---

The first capability, chosen the way the method says to choose it: frequency
times reversibility. It recurs every time anything runs, it verifies in under a
minute, and it costs nothing when it comes back wrong.

It also dogfoods. Its output is the input to the weekly meta-review, so a
workspace that cannot classify its own failures cannot improve, and this is the
capability that fails first and most visibly if the rest is broken.

`optimise: seconds` because this launch runs on kirocc, which meters no
dollars — optimising usd here would score every rung at zero and make the
ladder meaningless.

## prompt

A dispatch in this workspace failed its gate. The failure reason recorded was:

    {instance}

Classify it. Decide which of these it is, and cite the part of the reason text
that decides it:

  procedure   the agent followed the instructions and the instructions were
              wrong or incomplete. Fixable in SKILL.md or CONSTRAINTS.md.
  capability  the agent could not do the work at this rung. Fixable by moving
              the floor tier, not by more instruction.
  schema      the return schema asked for something the task cannot supply.
              Fixable in the launch.
  substrate   a credential, quota or connectivity problem wearing a quality
              failure's clothes. Fixable in the probe, never in CONSTRAINTS.md.

If it is `procedure`, draft the one-line constraint that would have prevented
it, in the CONSTRAINTS.md format. If it is not, leave `proposed_constraint`
null — a constraint that does not address the cause is worse than none.

## return

{
  "capability": "triage-failures",
  "instance": "<the failure reason, verbatim>",
  "claims": [
    {"statement": "class: procedure|capability|schema|substrate",
     "evidence": "<the exact substring of the reason that decides it>"}
  ],
  "proposed_constraint": "YYYY-MM-DD · ..." or null,
  "confidence": 0.0
}

## instances

- claims[0] has no evidence line
- claims must be a non-empty list
- confidence must be between 0 and 1, got 1.7
- HTTP 429: Too Many Requests
- verifier transport failure: timed out after 180s
- claims[0] evidence is not a locatable reference (want url, path#Lnn or path:nn): 'trust me'
- the agent answered in prose instead of the return schema
- unknown node type 'migration', expected one of ['capability', 'gate', 'tier']
