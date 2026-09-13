# AGENTS.md

For an agent working *on* this repository. If you are an agent dispatched *by*
it, you want `SKILL.md` — it is loaded into your prompt already.

## What this is

A workspace that runs recurring work on the cheapest model tier that still
passes an external gate, and learns which tier that is from recorded gate
verdicts. `docs/principles.md` is the reasoning; read it before changing
anything in `runner/`.

## Commands

```bash
python3 -m unittest discover -s tests -t .   # 35 tests, under a second, free
./ladder lint                                 # cheapest check; run after editing a launch
./ladder run <launch> --dispatch mock         # full pipeline, spends nothing
./ladder probe                                # what is actually reachable
```

No build step, no dependencies outside the standard library. Python 3.14.

## Conventions

- **Standard library only.** This workspace has to keep working when a package
  index does not.
- **Comments explain why, never what.** A comment that restates the line below
  it is noise. A comment recording the defect a line prevents is the reason the
  defect stays fixed.
- **Every threshold lives in code, not config.** Changing what "good enough"
  means should appear in a diff and go through review.
- **A new rule needs a test named after the defect it prevents.** Look at
  `tests/test_pipeline.py`: each test docstring is the failure it exists to
  catch.

## Invariants — do not break these without reading why

1. **Escalation is monotone in capability.** A failed rung never escalates to a
   rung of equal rank. Otherwise the retry is a re-roll, not a correction.
2. **A verifier never shares a model family with the generator.** Load-bearing;
   `docs/principles.md` §6.
3. **Transport failures stay out of the pass rate.** An outage must never push
   the routing policy up the ladder.
4. **`30-queries/needs-human.md` is append-only.** It holds decisions a human
   has already written.
5. **`20-graph/outcomes.jsonl` is append-only.** It is the entire learning
   substrate and the only thing that makes a run record answerable later.
6. **Anything that spends a dispatch appears in the run record** — including
   malformed replies, which are neither retried nor escalated.
7. **The meta-loop proposes diffs and never writes** to `SKILL.md`,
   `SCHEMA.md` or `CONSTRAINTS.md`.
8. **Nothing automated branches on a model's self-reported confidence.**

## Where things live

```
ladder              the CLI
runner/substrate.py probe: what is reachable, and what it costs to reach
runner/tiers.py     the ladder: rungs, costs, climb order, cross-family pairing
runner/policy.py    the learning: gate verdicts in, tier choice out
runner/dispatch.py  three adapters: kirocc (thin), claude -p (thick), mock
runner/gate.py      four stages, cheapest first
runner/launch.py    launch parsing and validation
runner/lint.py      launches checked against the gate that will judge them
runner/run.py       the loop
```
