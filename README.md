# ladder

Run recurring work on the cheapest model that still passes an external gate,
and let the run history — not a guess — decide which model that is.

`docs/principles.md` is why it is built this way, with the measurements and the
citations. This file is how to use it.

---

## Table of contents

1. [What this does](#1-what-this-does)
2. [The two ideas you need](#2-the-two-ideas-you-need)
3. [Five-minute start — spends nothing](#3-five-minute-start--spends-nothing)
4. [Every command](#4-every-command)
5. [The files, and who writes them](#5-the-files-and-who-writes-them)
6. [Writing a launch](#6-writing-a-launch)
7. [Running for real](#7-running-for-real)
8. [Reading the output](#8-reading-the-output)
9. [The weekly review](#9-the-weekly-review)
10. [Known limits](#10-known-limits)

---

## 1. What this does

You have work that recurs: triaging sources, summarising changelogs, classifying
failures, drafting migrations. Some of it a small fast model handles perfectly.
Some of it needs the expensive one. Nobody knows which is which, so everything
runs on the expensive one, forever.

`ladder` finds out. It runs each **capability** at a tier you declare, sends a
small fraction of dispatches one or more rungs cheaper as a trial, gates every
return outside the agent that produced it, and records the verdict. Once a
cheaper rung has enough trials above the bar, that rung becomes the default. If
it later degrades, the policy climbs back up on its own.

The thing that makes it safe is that nothing is promoted on a model's own
opinion of its work. Promotion is counted gate verdicts, and the gate is
outside the agent and — for stages 2 and 3 — outside its model family.

### What it produced on the machine it was built for

```
$ ladder probe
surface        lane   status     latency  detail
kirocc         thin   live         781ms  22 models, probed claude-haiku-4.5
claude-cli     thick  live        4282ms  probe cost $0.0240 (this is the harness tax, not the answer)
```

That second line is the whole design in one number. See §2.

---

## 2. The two ideas you need

### Lane before tier

The same model costs wildly different amounts depending on how you reach it.
Measured here, identical one-word prompt:

| Surface | Model | Input tokens | Cost |
|---|---|---|---|
| kirocc HTTP | `claude-haiku-4.5` | **91** | subscription-backed |
| `claude -p` | `claude-haiku-4.5` | **22,810** | **$0.0468** |
| `claude -p` | `claude-opus-4.5` | **22,804** | **$0.2297** |

`claude -p` drags ~22,800 tokens of harness — CLAUDE.md, hooks, plugin tool
schemas — into every dispatch, **identically on every model**. So:

- **thin lane** (kirocc, ~91 tokens): classification, extraction, scoring,
  verification, summarising. Cheap rungs pay off enormously.
- **thick lane** (`claude -p`, ~22,800 tokens): work that must read or edit the
  filesystem. Going cheap saves 4.9× on a floor that is 100% overhead.

Choosing a lane matters roughly 250× more than choosing a model. A launch
declares its lane, and a capability cannot change lane without a human.

### A tier is a (surface, model) pair

Not a model. `haiku-4.5` and `cli-haiku-4.5` are the same weights and two
different rungs, because the fixed cost differs by 250×. `ladder tiers` prints
both ladders.

---

## 3. Five-minute start — spends nothing

```bash
cd ~/Projects/ladder

./ladder probe                 # what is actually reachable, right now
./ladder tiers --w-usd 0 --w-seconds 1   # the ladder, cheapest-first by latency
./ladder launches              # what is defined
./ladder lint                  # the cheapest check. run it after every edit
./ladder plan triage-failures  # what a run WOULD dispatch, without dispatching
./ladder run triage-failures --dispatch mock   # fake agents, zero cost
./ladder policy                # what the system now believes
```

The mock is the default dispatcher on purpose. It produces synthetic returns
with a deliberate defect rate and a stubborn slice that never passes, so the
gate, the retry, the escalation and the human queue all get exercised for free.
**Run the mock before every real run.** A broken launch caught by fake agents
costs nothing.

To undo a mock run:

```bash
rm -rf 40-runs/*.md 10-returns/* 20-graph/outcomes.jsonl 30-queries/needs-human.md
```

Run the tests the same way — they spend nothing and take under a second:

```bash
python3 -m unittest discover -s tests -t .
```

---

## 4. Every command

### `ladder probe [--fast]`

Measures which dispatch surfaces answer. A catalog is a claim; this is the
capability. Writes `20-graph/substrate.json`, which selection reads — a rung
whose surface is dead is never dispatched into.

`--fast` probes only the two surfaces known to work. Run the full probe weekly:
"dead" is a credential state, not a property, and this is the only thing that
will notice when one starts working.

### `ladder tiers [--w-usd N] [--w-tokens N] [--w-seconds N]`

Prints both ladders, cheapest-first under the weights you give. **Weights
matter:** kirocc meters no dollars, so `--w-usd 1` scores every kirocc rung at
zero and the ordering becomes meaningless. Use `--w-seconds 1` there.

### `ladder lint [launch]`

The cheapest check in the workspace. Runs the launch's own example return
through the gate that will judge its output, and reports launches that cannot
possibly pass their own gate, retries with nowhere to climb, and gates with no
cross-family verifier available. Run it after editing any launch.

### `ladder plan <launch>`

What a run would dispatch, the rung it would pick and why, the escalation path,
and the verifier rungs. Spends nothing.

### `ladder run <launch> [--dispatch mock|auto|kirocc|claude-cli] [--gate script|checkable|full] [--cap N] [--seed N]`

Runs it. `--dispatch mock` is the default and free. `--gate script` is gate 1
only — also free, and the right setting for a first real run: find out whether
the launch works before paying to verify its output.

### `ladder policy`

What the system believes about each capability: trials and gate pass rate per
rung, whether a rung is decided, and any `needs_split` finding.

### `ladder review [--days 7] [--out FILE]`

Assembles the weekly meta-review prompt from the run records. Feed it to a
fresh-context agent. **It returns a diff. You apply it.**

---

## 5. The files, and who writes them

One author per directory. Numeric prefixes fix the write order.

### You write these

| File | What it is |
|---|---|
| `SCHEMA.md` | node types, edge types, gate stages, thresholds. The contract. |
| `SKILL.md` | the procedure, loaded at the top of every dispatch |
| `CONSTRAINTS.md` | corrections that carry forward, loaded before `SKILL.md` |
| `00-launches/*.md` | one question per sweep |

### The system writes these — do not edit by hand

| Path | What it is |
|---|---|
| `10-returns/<run-id>/` | raw returns, one file per dispatch |
| `20-graph/outcomes.jsonl` | **append-only.** One row per dispatch. The entire learning substrate. |
| `20-graph/substrate.json` | last probe result |
| `40-runs/<run-id>.md` | the audit trail, and the meta-review's only input |

### The one shared file

`30-queries/needs-human.md` — the system appends; you write decisions under the
entries. Nothing ever rewrites it, because it is the only file holding work a
human has already done.

---

## 6. Writing a launch

A launch is **not** a list of work with a model attached. It names a capability
and supplies instances; which rung runs them is the policy's answer, derived
from history. That is why next month the same file dispatches somewhere else
without anyone editing it.

```markdown
---
capability: triage-failures        # the node. A recurring CLASS of work.
question: Which failures are procedure defects?
lane: thin                         # thin | thick — see §2
floor_tier: sonnet-5               # pessimistic start. Never the cheapest rung.
risk_class: reversible             # reversible | costly | irreversible
gate: script                       # script | checkable | full
optimise: seconds                  # usd | seconds | tokens | balanced
max_dispatches: 40
max_attempts: 2
---

## prompt
Classify {instance}. Cite the substring that decides it.

## return
{"capability":"...","instance":"...","claims":[{"statement":"...","evidence":"..."}]}

## instances
- one per line
```

Three fields decide more than they look like they do:

- **`floor_tier`** is the policy's ceiling of pessimism. A capability with no
  history runs here. Never set it to the cheapest rung: a new capability
  failing there teaches you about the rung, not the capability.
- **`risk_class: irreversible`** — anything that sends, pays or publishes —
  **disables downward exploration entirely** and requires `gate: full`.
  Exploration is a controlled experiment, and an experiment you cannot undo is
  not controlled.
- **`optimise`** names the objective. Get it wrong on a subscription-backed
  surface and every rung scores identically.

Then: `./ladder lint <name>` before you run it.

### Evidence: the thing launches get wrong

Every claim needs an evidence line, and there are exactly two legitimate kinds:

- a **locator** — a URL, `path#Lnn`, or `path:nn`; something a third party can
  open;
- a **quotation** — a verbatim substring of material the prompt supplied,
  checked against the prompt for free at gate 1. A quotation that is not in the
  source is a fabricated citation and is rejected without spending a token.

The first live run of this workspace failed because its launch asked for
quotations while the gate accepted only locators. Ten quality failures, two
escalations, and the agents had done nothing wrong. `ladder lint` exists to
catch that class before a run spends anything.

---

## 7. Running for real

Checklist before a first real run:

1. `./ladder probe` — is anything alive?
2. `./ladder lint <launch>` — clean?
3. `./ladder run <launch> --dispatch mock` — does the pipeline work?
4. `./ladder plan <launch>` — is the rung it picked the one you expected?
5. `./ladder run <launch> --dispatch kirocc --gate script --cap 8` — small, gate 1 only.
6. Read `40-runs/`. Then widen the cap, then raise the gate.

### What it costs

In the thin lane, dispatches are subscription-backed: they cost latency and
quota, not dollars. In the thick lane, a dispatch costs **$0.047 (haiku) to
$0.23 (opus) before your task text is counted.** `ladder run` prints the metered
total and the input-token total every time.

### Concurrency

Thin-lane concurrency defaults to 4 with jittered exponential backoff on
429/5xx. This is not conservatism for its own sake: a 16-way fan-out during
development took the proxy's upstream to 502 for every subsequent request. The
bottleneck is a shared quota, not your machine.

---

## 8. Reading the output

`40-runs/<run-id>.md` separates the four things that can happen to a dispatch,
because they go to different places:

| Class | Meaning | What happens |
|---|---|---|
| **passed** | cleared every gate stage the launch asked for | recorded, counts toward promotion |
| **quality failed** | a real return that failed a gate | retried **with the reason**, one rung up; to the human queue at the cap |
| **transport failed** | never answered — timeout, 401, 429, 502 | retried **clean**; **excluded from the pass rate**; never reaches the human queue |
| **malformed** | answered, but not in the schema | logged, dropped, not retried; **counts against the rung** |

That third row is load-bearing. During development the proxy's upstream went
502 mid-sweep; all 16 dispatches were classified `transport`, kept out of the
pass rate, and the human queue stayed empty. Had they been counted as quality
failures, the policy would have climbed the ladder — expensively, for a reason
that had nothing to do with any model's capability.

`30-queries/needs-human.md` is your five minutes a day. It is small on purpose:
only twice-failed *quality* failures reach it, because a queue containing
timeouts is a log, and logs do not get read.

---

## 9. The weekly review

```bash
./ladder review --days 7
```

Assembles the run records and the current policy state into a prompt for one
fresh-context agent. It looks for failure reasons appearing twice or more,
capabilities escalated repeatedly, rungs stuck below the bar, and transport
failures clustering on one surface.

**It returns a diff to `SKILL.md` / `SCHEMA.md` / `CONSTRAINTS.md`. You apply
it.** It never writes to those files. An agent that can edit its own
constraints will eventually edit away the inconvenient one, and it will have a
good explanation ready.

The automatic half is narrower and safer: tier policy moves on counted gate
outcomes under a rule written in `SCHEMA.md`. Numbers move the policy; only a
human moves the rules that decide what the numbers mean.

---

## 10. Known limits

- **The unit of learning is the capability, not the query.** There is no
  per-query difficulty estimate. If a capability's instances vary too much for
  one tier, `needs_split` says so and the fix is to split the capability — no
  routing change repairs it.
- **Self-reported confidence is inadmissible anywhere automated.** A return may
  carry it for the human queue. Nothing branches on it. See
  `docs/principles.md` §5.
- **The thick lane has only one model family**, so `gate: full` is impossible
  there — the linter catches it. Cross-family verification currently requires
  the thin lane.
- **Expect the boring end of the savings range.** The routing literature's
  headline numbers are softer than they look; RouteLLM's 3.66× is measured
  against a random router at equal quality, not against always calling the
  strong model, and the same router delivers 1.41× on MMLU. The reason to run
  this is not a headline multiple — it is that the run record makes the
  question answerable on *your* workload.
- **This does not fix a badly defined capability**, and no amount of graph
  structure will.
