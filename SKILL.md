# SKILL.md · the procedure

Loaded at the top of every dispatch, before the task text, on every rung. A
prompt is a sample of intent; this file is the intent itself, versioned and
reviewed.

`CONSTRAINTS.md` overrides this file wherever the two disagree.

## STEPS

1. **read** — the capability, the instance, and the constraints block above.
2. **work** — do the thing the task asks, and only that thing.
3. **cite** — attach a locatable evidence line to every claim you make.
4. **self-check** — run the checklist below before returning.
5. **return** — the return schema, nothing else.

## DECISION RULES

- A claim with no evidence line is not a claim. Drop it or flag it; never
  assert it.
- Evidence must be **locatable by someone who is not you**: a URL, a
  `path#Lnn`, or a `path:nn`. "Based on the code" is not evidence, and it is
  rejected by a script before a verifier ever sees it.
- Answer the question that was asked. A better question you noticed goes in
  `notes`, not in place of the answer.
- If the instance is ambiguous, return it **flagged**, do not guess. A guess
  that passes the gate is worse than an escalation that does not.
- Do not repair the task. If the instance is malformed, say so and return.
- Report what you found, including when what you found is nothing.

## SELF-CHECK (before returning)

- [ ] every claim has a `statement` and a locatable `evidence` line
- [ ] no claim asserts more than its evidence shows
- [ ] the output is the schema and nothing else: no preamble, no explanation,
      no markdown outside the JSON
- [ ] if a previous attempt's failure reason was supplied, the specific thing
      it named is fixed — not worked around, not re-explained

## ON A RETRY

When the task carries a `YOUR PREVIOUS ATTEMPT FAILED THE GATE` block, that
text is the gate's actual reason, not a hint. Correct exactly what it names.
Returning the same shape with different wording fails the same way and spends
the last attempt you have.

## OUTPUT

The return schema supplied in the task, and nothing else.

A `confidence` field, where the schema has one, is carried for a human to read.
**Nothing automated branches on it** — see SCHEMA.md. Do not inflate it to get
through the gate; the gate does not read it.
