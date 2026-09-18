# Contributing to Ladder

Thanks for considering a contribution. This project has a small, specific
vocabulary and a set of invariants that are each a defect that happened before
it became a rule. Reading the docs first will save you from making edits that
are confidently wrong in ways that are hard to review.

---

## Getting started

```bash
git clone <repo-url> && cd ladder
python3 -m unittest discover -s tests -t .   # 79 tests, <0.1s, free
./ladder run triage-failures --dispatch mock  # full pipeline, spends nothing
```

**Prerequisites:** Python 3.11+. Nothing else. No `pip install`, no build
step, no dependencies outside the standard library.

The mock dispatch exercises the full pipeline — tier selection, dispatch,
gate, recording — without spending tokens or dollars. If the tests pass and
the mock run completes, your checkout is healthy.

---

## Read before you code

The vocabulary here is specific. *Surface*, *lane*, *rung*, *capability*, and
*deferred* all mean particular things, and guessing at them produces edits that
look right but break invariants. Read these three, in this order:

1. **[docs/GLOSSARY.md](docs/GLOSSARY.md)** — the terms. 4 minutes.
2. **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the module map, the
   flowchart, which module owns which decision, how to extend each part.
   10 minutes.
3. **[docs/principles.md](docs/principles.md)** — *why*. Read this before
   changing anything in `runner/gate.py` or `runner/policy.py`; most of what
   looks like an arbitrary choice there is load-bearing and cited. 25 minutes.

Then skim `tests/test_pipeline.py`. Every test docstring names the defect it
prevents — that's the fastest route to understanding the constraints.

---

## Code conventions

- **Standard library only.** No pip dependencies, no exceptions. This
  workspace has to keep working when a package index does not.

- **Comments explain *why*, never *what*.** A comment that restates the line
  below it is noise. A comment recording the defect a line prevents is the
  reason the defect stays fixed.

- **Every threshold lives in code, not config.** Changing what "good enough"
  means should appear in a diff and go through review. No environment
  variables, no config files for promotion thresholds or pass-rate bars.

- **A new rule needs a test named after the defect it prevents.** Look at
  `tests/test_pipeline.py`: each test docstring is the failure it exists to
  catch. If you can't name the defect, the rule may not be needed.

- **No formatter is enforced**, but clean Python style is expected. Readable
  code, consistent with what's already there.

---

## Testing

```bash
python3 -m unittest discover -s tests -t .
```

79 tests, under a tenth of a second, no network, no tokens, completely free.
Run them before every commit.

The tests cover:

- **Tier ordering invariants** — escalation is monotone, cross-family pairing
  holds, across every rung in the ladder.
- **Policy learning** — promotion, demotion, exploration, and floor-tier
  behaviour, simulated over multi-run sequences.
- **Gate stages** — each stage's accept/reject boundary, failure
  classification, the cross-family constraint, gate-3 refusal.
- **Dispatch** — transport error handling, malformed-reply classification,
  the mock adapter's contract.
- **Launch validation** — the linter catches launches that can't pass their
  own gate.

**Naming convention:** every test docstring names the defect it prevents, not
what the test does. `test_transport_excluded_from_pass_rate` tells you the
bug. If your new test can't name a defect, it may be testing plumbing
rather than behaviour.

---

## The invariants

These are each a bug that happened. Do not break any of them without reading
the rationale in [docs/principles.md](docs/principles.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

1. **Escalation is monotone in capability.** A failed rung never escalates to
   a rung of equal rank. Otherwise the retry is a re-roll, not a correction.
2. **A verifier never shares a model family with the generator.** Load-bearing;
   `docs/principles.md` §6.
3. **Gate 3 refuses rather than using a weaker judge.** A weak judge passes
   exactly what the gate exists to catch, while reporting success.
4. **Transport failures stay out of the pass rate.** An outage must never push
   routing up the ladder.
5. **`needs-human.md` and `outcomes.jsonl` are append-only.** They hold
   decisions and evidence that must never be silently rewritten.
6. **Anything that spends a dispatch appears in the run record** — including
   malformed replies, which are neither retried nor escalated.
7. **The meta-loop proposes diffs and never writes** to `SKILL.md`,
   `SCHEMA.md`, or `CONSTRAINTS.md`.
8. **Nothing automated branches on high self-reported confidence.** Low
   confidence may escalate; high confidence is never a pass, a tiebreak,
   or a threshold.
9. **Every writer resolves its path at call time**, never as a module-level
   default argument.

---

## How to add things

### Add a model

Add a `Tier(...)` to `LADDER` in `runner/tiers.py`. Set `family` correctly —
it is what the cross-family constraint keys on. Run the tests; the invariant
tests sweep every rung, so a badly ranked or mis-familied addition fails
immediately.

### Add a surface

1. Write a `probe_*()` function in `runner/substrate.py` that sends one real
   request and returns health data.
2. Add it to `probe_all()`.
3. Write a dispatcher class in `runner/dispatch.py` with a
   `run(works) -> [Return]` method, and register it in `DISPATCHERS`.
4. Add tiers pointing at the new surface in `runner/tiers.py`.

The `Return` contract is the only thing that matters: set
`transport_error=True` when the model **never answered**, and leave
`record=None` when it answered with something unparseable. Getting that
distinction wrong is the one way to corrupt the learning substrate.

**Note on `kirocc`:** the existing thin-lane surface talks to a local
OpenAI-compatible HTTP proxy at `127.0.0.1:3456`. If you use a different
OpenAI-compatible endpoint, point it there or add your own surface — the
dispatch interface is the same either way.

### Add a gate stage

Gates are callables returning `(ok, reasons, retryable, verifier_tier)`.
Insert the new stage into `run_gate()` in `runner/gate.py` **in cost order** —
the ladder's economics depend on free checks running first.

### Add a capability (write a launch)

1. Write a launch file in `00-launches/`. A launch names a capability and
   supplies instances; it **never** names a model — the policy decides the
   rung from history.
2. Run `./ladder lint <name>` — it checks whether the launch's own example
   can pass its own gate.
3. Run `./ladder run <name> --dispatch mock` — exercises the full pipeline
   at zero cost.
4. Run a small real dispatch with `--gate script` to confirm the surface
   handles it.

---

## Pull request process

Before opening a PR:

1. **Run the tests.** `python3 -m unittest discover -s tests -t .` — all 79
   must pass.
2. **Run lint** on any launch you touched. `./ladder lint <name>`.
3. **Run a mock.** `./ladder run <name> --dispatch mock` on any launch
   affected by your change.
4. **Describe what changed and what defect the change prevents.** "Refactored
   X" is not useful. "Gate 2 was accepting fabricated quotations when the
   source text contained Unicode whitespace" is.

If your change adds a threshold, a rule, or a constraint, it needs a test
named after the defect it prevents. If your change touches `gate.py` or
`policy.py`, say which section of `docs/principles.md` you read and why the
invariants still hold.

---

## Reporting issues

Open an issue on GitHub. Include:

- **The command you ran** — the full `./ladder ...` invocation.
- **The output** — paste it, don't describe it.
- **Your Python version** — `python3 --version`.
- **Your OS**, if relevant (surface probing is platform-sensitive).

If the issue involves a gate verdict or policy decision, include the relevant
lines from `20-graph/outcomes.jsonl` if you have them — they're the evidence
the system acted on.
