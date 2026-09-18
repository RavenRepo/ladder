# Getting started

A guided first hour. Every command here **costs nothing** — no tokens, no
dollars — so you can run all of it before you understand any of it.

If a term looks odd (*rung*, *lane*, *capability*, *deferred*), it's in
[GLOSSARY.md](GLOSSARY.md). Don't guess; the words mean specific things.

**Prerequisites:** Python 3.11+. That's it. No `pip install`, no build step,
no dependencies outside the standard library.

---

## The problem this solves, in one paragraph

You have work that repeats. Some of it a small fast model handles perfectly;
some of it needs the expensive one. Nobody knows which is which, so everything
runs on the expensive one forever. This workspace finds out — by occasionally
trying a cheaper model, checking the result with something *outside* the model
that produced it, and keeping score. When the cheap rung has proven itself
enough times, it becomes the default. If it later degrades, the system climbs
back up on its own.

Nothing is ever promoted because a model said it was confident.

---

## Step 1 — See what's actually reachable

```bash
cd ladder
./ladder probe
```

```
surface        lane   status     latency  detail
kirocc         thin   dead             -  claude-haiku-4.5 did not answer: HTTP Error 502: Bad Gateway
claude-cli     thick  live        5039ms  probe cost $0.0249 (this is the harness tax, not the answer)

1/2 surfaces live. Health written to 20-graph/substrate.json
```

**Read this output carefully — it teaches you the whole design.**

- A *surface* is a way to reach models. `kirocc` is a local HTTP proxy;
  `claude-cli` shells out to `claude -p`.
- `probe` doesn't read a config file. It **actually sends a request** and
  reports what came back. A model catalog is a claim; this is the capability.
- That `$0.0249` is the cost of asking `claude -p` to say one word. It's ~100%
  overhead — CLAUDE.md, hooks and tool schemas loaded before your prompt is
  even read. Remember that number.
- When something is `dead`, selection **refuses to dispatch into it** rather
  than discovering it one failed request at a time.

> **If everything says dead:** that's fine for this walkthrough. Every step
> below uses fake agents.

---

## Step 2 — Look at the ladder

```bash
./ladder tiers --w-usd 0 --w-seconds 1
```

```
thin lane
  rung             family  rank      usd   tok_in    sec  surface
  haiku-4.5        claude  4      0.0000       91   0.74  kirocc
  gpt-5.6-sol      gpt     2      0.0000       91   0.75  kirocc
  gpt-5.6-luna     gpt     3      0.0000       91   0.78  kirocc
  opus-5           claude  0      0.0000       91   1.50  kirocc
  sonnet-5         claude  2      0.0000       91   1.69  kirocc

thick lane
  rung             family  rank      usd   tok_in    sec  surface
  cli-haiku-4.5    claude  4      0.0468   22,810   5.00  claude-cli
  cli-sonnet-4.5   claude  2      0.1000   22,800   6.00  claude-cli
  cli-opus-4.5     claude  0      0.2297   22,804   8.00  claude-cli
```

Three things to notice:

1. **`haiku-4.5` and `cli-haiku-4.5` are the same model** — and 91 vs 22,810
   input tokens. That 250× gap is why a "tier" here is a *(surface, model)
   pair*, never a model on its own.
2. **`rank 0` is strongest.** Sorting is by *cost*, so the list is not in rank
   order — that's the point. Cheapest first is the order you try things.
3. **The `--w-*` flags matter.** `kirocc` is subscription-backed, so every
   `usd` is `0.0000`. Optimise for dollars there and every rung ties. Try
   `./ladder tiers` with defaults and watch the thin lane become meaningless.

---

## Step 3 — Read a launch

```bash
./ladder launches
cat 00-launches/triage-failures.md
```

A **launch** is one sweep of work. The important thing about it:

> **A launch does not name a model.**

It names a *capability* and supplies *instances*. Which model runs them is the
policy's answer, derived from history. That's what makes it a workflow rather
than a script — next month the same file dispatches somewhere else without
anyone editing it.

The front block:

```yaml
capability: triage-failures     # the recurring class of work
lane: thin                      # thin | thick — pick this FIRST
floor_tier: sonnet-5            # where it starts when nothing is known
risk_class: reversible          # reversible | costly | irreversible
gate: script                    # script | checkable | full
optimise: seconds               # what "cheapest" means for this launch
```

---

## Step 4 — Lint it (always do this)

```bash
./ladder lint
```

```
ok   triage-failures
```

This is the cheapest check in the workspace, and it exists because of a real
failure. The very first live run here failed **10 of 16 dispatches** — and
every failure was the *gate's* fault, not the models'. The launch asked agents
to quote a substring; the gate only accepted URLs. Both files were internally
consistent and they contradicted each other.

So `lint` doesn't check style. It **takes the launch's own example return and
runs it through the gate that will judge its output.** If the example can't
pass, nothing the launch dispatches can either.

Try breaking it on purpose — delete the `"evidence"` field from the `## return`
block in the launch and re-run `lint`. It will tell you exactly what would have
happened.

---

## Step 5 — See what a run *would* do

```bash
./ladder plan triage-failures
```

```
triage-failures: triage-failures · 8 instances
lane=thin floor=sonnet-5 risk=reversible gate=script

would dispatch at: sonnet-5 — no history; running at the declared floor tier
escalation path:   opus-5 -> opus-4.5
```

`plan` spends nothing. `"no history"` means this capability has never run, so
it starts at the **floor tier** — deliberately pessimistic. A brand-new
capability failing on the cheapest model would teach you about *the model*, not
about the capability.

The escalation path is where a failure climbs to. Note it's strictly *stronger*
— a failed rung never escalates sideways, or the retry would be the same coin
flipped twice.

---

## Step 6 — Run it with fake agents

```bash
./ladder run triage-failures --dispatch mock --seed 7
```

```
20260913T141256Z-triage-failures
  dispatched 11 · passed 6 · quality-failed 5 · malformed 0 · transport 0
  explored cheaper 2 · escalated to human 2
  cost $0.0000 · 1,001 input tokens · 0.0s
  stopped: work list exhausted
  record: 40-runs/20260913T141256Z-triage-failures.md
```

**8 instances produced 11 dispatches.** The extra 3 are retries — failures
climb a rung and try again, carrying the reason they failed.

The mock isn't a toy. It produces a deliberate defect rate plus a *stubborn*
slice that never passes, so the gate, the retry ladder, the escalation and the
human queue all get exercised for free. **Run the mock before every real run.**

Now read what it produced:

```bash
cat 40-runs/*.md              # the audit trail
cat 30-queries/needs-human.md # your five minutes a day
ls 10-returns/*/              # every raw return
```

`needs-human.md` is deliberately small. Only twice-failed *quality* failures
reach it — a queue containing timeouts is a log, and logs don't get read.

---

## Step 7 — See what it learned

```bash
./ladder policy
```

```
triage-failures
  opus-5              33% over   3 trials  3/12
  sonnet-5            50% over   6 trials  6/12
  haiku-4.5          100% over   2 trials  2/12
```

`3/12` means "3 trials, needs 12 before this rung can be trusted". Nothing gets
promoted on 2 good results — **promotion requires a sample size, demotion does
not.** Being wrong about "this cheap rung is fine" costs quality on every future
run; being wrong about "go back up" costs money on a few.

Run step 6 a few more times with different `--seed` values and watch the trial
counts climb.

---

## Step 8 — Undo everything

```bash
./ladder clean
```

```
removed 10-returns/20260913T141256Z-triage-failures
removed 40-runs/20260913T141256Z-triage-failures.md
removed 20-graph/outcomes.jsonl
removed 20-graph/substrate.json
removed 30-queries/needs-human.md

5 generated artifacts removed. Outcome history is now empty — the policy has forgotten everything.
```

Use the command, not `rm -rf 40-runs/*.md 10-returns/*` — that glob also eats
the README documenting each directory, and `git add -A` then quietly stages the
deletion. That happened here, twice, before anyone noticed.

All of those are generated and gitignored. A fresh clone starts with an empty
outcome history on purpose — inheriting someone else's would mean believing
things about *their* workload.

---

## Step 9 — Run the tests

```bash
python3 -m unittest discover -s tests -t .
```

```
Ran 79 tests in 0.081s
OK
```

Under a tenth of a second, zero tokens. **Every test docstring names the defect
it prevents** — reading them is the fastest way to understand why the code is
shaped the way it is. Start with `TestLadderInvariants`.

---

## Where to go next

| You want to… | Read |
|---|---|
| know what a word means | [GLOSSARY.md](GLOSSARY.md) |
| see how the pieces connect | [ARCHITECTURE.md](ARCHITECTURE.md) |
| understand *why* it's built this way | [principles.md](principles.md) |
| look up a command or a file | [../README.md](../README.md) |
| change the code | [../AGENTS.md](../AGENTS.md) |
| change the rules | [../SCHEMA.md](../SCHEMA.md) |

---

## Running for real (when a surface is live)

Work up in this order. Each step is cheap and catches things the next one
would charge you for.

```bash
./ladder probe                                     # is anything alive?
./ladder lint <launch>                             # clean?
./ladder run <launch> --dispatch mock              # pipeline works?
./ladder plan <launch>                             # right rung?
./ladder run <launch> --dispatch kirocc --gate script --cap 8   # small, gate 1 only
# read 40-runs/, then widen --cap, then raise --gate
```

`--gate script` is free (no verifier calls). Use it for a first real run: find
out whether the launch works before paying to verify its output.

---

## Five things that will confuse you

1. **"Cheapest" is ambiguous, on purpose.** Three currencies — `usd`,
   `tokens_in`, `seconds` — and the launch declares which one it means.
   `kirocc` costs $0, so optimising dollars there ranks nothing.

2. **`transport` failures don't count against a model.** A 502 says nothing
   about whether a model is good at a task. If outages counted as failures,
   an afternoon of rate limits would push routing to the most expensive rung
   and pin it there.

3. **A verifier is never the same model family as the generator.** Claude work
   is checked by GPT and vice versa. Not a preference — a model is worst at
   spotting the kind of error it makes itself.

4. **High confidence buys nothing.** A model saying "0.95" is ignored entirely.
   A model saying "0.3" escalates it a rung. Stated confidence is biased toward
   overconfidence, so only the low direction carries information.

5. **The weekly review never edits anything.** It returns a diff; a human
   applies it. In a published experiment, a self-improving agent fabricated its
   tool-use logs *and deleted the markers used to detect its hallucinations*.
   A system allowed to edit its own instrumentation eventually will.
