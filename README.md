# ladder

Run recurring LLM work on the cheapest model tier that still passes an external gate, and let the run history -- not a guess -- decide which model that is.

Everything runs on the expensive model because nobody knows which tasks need it. `ladder` finds out empirically: it occasionally trials a cheaper rung, checks every result with a verifier *outside* the model that produced it, and promotes or demotes based on counted verdicts. Nothing is ever promoted because a model said it was confident.

---

## Why

LLM routing today is either a fixed config ("always use GPT-4") or a learned router that predicts query difficulty. The fixed config overspends. The learned routers -- across 400K instances and 33 models -- frequently [fail to beat Best-Single-Model](https://arxiv.org/abs/2601.07206), and the safe down-routing ceiling may be [~20% of queries](https://arxiv.org/abs/2404.14618). Per-query difficulty estimation itself [converges far below oracle](https://arxiv.org/abs/2606.07587) because routers learn global trends rather than query-specific signal.

`ladder` takes a different approach: **don't predict difficulty -- measure it.** Run work at your declared floor, explore cheaper rungs on a fraction of dispatches, gate every return with a cross-family verifier, and let counted pass rates drive promotion. The unit of learning is the *capability* (a recurring class of work), not the individual query, because that is where the signal actually lives.

The run record answers the question for *your* workload. "The expensive rung was right all along" is a valid, cheaply obtained result.

---

## Quick start

Python 3.11+. No dependencies outside the standard library. No build step.

```bash
git clone https://github.com/RavenRepo/ladder.git && cd ladder
./ladder probe                                    # what surfaces are reachable
./ladder lint                                     # check all launches against their own gates
./ladder run triage-failures --dispatch mock       # full pipeline, fake agents, zero cost
./ladder policy                                   # what the system now believes
python3 -m unittest discover -s tests -t .        # 79 tests, <0.1s, zero tokens
```

Every command above is free. See [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) for the full guided walkthrough.

---

## How it works

### 1. Lane before tier

The same model costs wildly different amounts depending on how you reach it (its *surface*). Measured here, identical one-word prompt:

| Surface | Model | Input tokens | Cost |
|---|---|---|---|
| HTTP endpoint | `claude-haiku-4.5` | **91** | subscription-backed |
| `claude -p` | `claude-haiku-4.5` | **22,810** | **$0.0468** |
| `claude -p` | `claude-opus-4.5` | **22,804** | **$0.2297** |

`claude -p` drags ~22,800 tokens of harness -- CLAUDE.md, hooks, plugin tool schemas -- into every dispatch, identically on every model. Choosing a lane matters roughly **250x more** than choosing a model.

- **thin lane** (HTTP endpoint, ~91 tokens): classification, extraction, scoring, verification, summarising. Cheap rungs pay off enormously.
- **thick lane** (`claude -p`, ~22,800 tokens): work that must read or edit the filesystem. Going cheap saves 4.9x on a floor that is 100% overhead.

A tier is a *(surface, model)* pair, not a model. `haiku-4.5` and `cli-haiku-4.5` are the same weights and two different rungs, because the fixed cost differs by 250x.

### 2. External gate with cross-family verification

Every return is judged by something outside the model that produced it. The gate has three stages, run cheapest-first:

1. **script** (free) -- structural checks, evidence validation against the prompt
2. **checkable** -- a cheap model from a *different* family verifies claims
3. **judgment** -- a peer-or-stronger model from a *different* family judges quality

Cross-family verification is a hard constraint: Claude checks GPT's work, GPT checks Claude's. A model never judges its own output, and a weaker verifier never substitutes for a stronger one.

### 3. The learning loop

```
explore cheaper rung --> dispatch --> gate verdict --> record outcome --> promote or demote
```

- A capability with no history starts at its declared **floor tier** (deliberately pessimistic)
- 15% of dispatches trial the cheapest undecided rung
- Promotion requires a sample size (12 trials above the bar); demotion does not
- If a capability's instances vary too much for one tier, `needs_split` flags it

### 4. Four failure classes

| Class | Meaning | What happens |
|---|---|---|
| **passed** | cleared every gate stage | recorded, counts toward promotion |
| **quality failed** | real return, failed a gate | retried with the reason, one rung up; to human queue at retry cap |
| **transport failed** | never answered (timeout, 401, 502) | retried clean; **excluded from pass rate** |
| **malformed** | answered, not in the schema | logged, dropped, counts against the rung |
| **deferred** | passed but confidence < 0.5 | escalated one rung without consuming a retry; excluded from pass rate |

Transport failures staying out of the pass rate is load-bearing: an outage must never push routing up the ladder.

---

## Command reference

| Command | What it does | Spends? |
|---|---|---|
| `ladder probe [--fast]` | Measure which surfaces answer; writes `substrate.json` | One request per surface |
| `ladder tiers [--w-usd N] [--w-tokens N] [--w-seconds N]` | Print both ladders, cheapest-first under given weights | No |
| `ladder launches` | List defined launches | No |
| `ladder lint [launch]` | Run a launch's example through its own gate | No |
| `ladder plan <launch>` | Show what a run would dispatch and why | No |
| `ladder run <launch> [--dispatch mock\|auto\|...] [--gate ...] [--cap N]` | Execute a run (`mock` is the default and free) | Depends on dispatcher |
| `ladder clean` | Delete generated artifacts, keep READMEs; **erases policy memory** | No |
| `ladder review [--days 7] [--out FILE]` | Build the weekly meta-review prompt | No |
| `ladder policy` | Show current pass rates and promotion state per capability | No |

---

## Writing a launch

A launch names a **capability** and supplies instances. Which model runs them is the policy's answer, derived from history -- next month the same file dispatches somewhere else without anyone editing it.

```yaml
---
capability: triage-failures        # recurring class of work
question: Which failures are procedure defects?
lane: thin                         # thin | thick
floor_tier: sonnet-5               # pessimistic start; never the cheapest rung
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

- **`floor_tier`**: where a new capability starts. Never set it to the cheapest rung -- a new capability failing there teaches you about the rung, not the capability.
- **`risk_class: irreversible`**: anything that sends, pays, or publishes. Disables downward exploration entirely and requires `gate: full`. An experiment you cannot undo is not controlled.
- **`optimise`**: names the cost objective. Get it wrong on a subscription-backed surface and every rung scores identically.

Then: `./ladder lint <name>` before you run it.

---

## The files, and who writes them

One author per directory. Numeric prefixes fix the write order.

### You write these

| File | What it is |
|---|---|
| `SCHEMA.md` | Node types, edge types, gate stages, thresholds. The contract. |
| `SKILL.md` | The procedure, loaded at the top of every dispatch |
| `CONSTRAINTS.md` | Corrections that carry forward, loaded before `SKILL.md` |
| `00-launches/*.md` | One question per sweep |

### The system writes these -- do not edit by hand

| Path | What it is |
|---|---|
| `10-returns/<run-id>/` | Raw returns, one file per dispatch |
| `20-graph/outcomes.jsonl` | **Append-only.** One row per dispatch. The entire learning substrate. |
| `20-graph/substrate.json` | Last probe result |
| `40-runs/<run-id>.md` | The audit trail, and the meta-review's only input |

### The one shared file

`30-queries/needs-human.md` -- the system appends; you write decisions under the entries. Nothing ever rewrites it, because it is the only file holding work a human has already done.

---

## Running for real

Checklist before a first real run:

1. `./ladder probe` -- is anything alive?
2. `./ladder lint <launch>` -- clean?
3. `./ladder run <launch> --dispatch mock` -- does the pipeline work?
4. `./ladder plan <launch>` -- is the rung it picked the one you expected?
5. `./ladder run <launch> --dispatch auto --gate script --cap 8` -- small, gate 1 only
6. Read `40-runs/`. Then widen the cap, then raise the gate.

### Surfaces and `kirocc`

`ladder` talks to models through **surfaces** -- transport mechanisms that expose an OpenAI-compatible HTTP API or a CLI harness.

The default thin-lane surface is `kirocc`, a local OpenAI-compatible HTTP proxy. You can substitute any OpenAI-compatible endpoint: [Ollama](https://ollama.com/), [vLLM](https://github.com/vllm-project/vllm), [LiteLLM](https://github.com/BerriAI/litellm), or a direct provider API. To add or change surfaces, edit the `probe_*()` functions in `runner/substrate.py` and register corresponding dispatchers in `runner/dispatch.py`.

The thick-lane surface shells out to `claude -p` (the Claude Code CLI). Thick-lane dispatches carry ~22,800 tokens of harness overhead regardless of the model.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | Guided first hour -- every command is free |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Data flow, module map, mermaid diagram, how to extend |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | Precise definitions: rung, lane, capability, deferred, ... |
| [docs/principles.md](docs/principles.md) | Why it's built this way -- measurements and 37 citations |
| [SCHEMA.md](SCHEMA.md) | The contract: node types, edge types, thresholds |
| [AGENTS.md](AGENTS.md) | Conventions and invariants for changing the code |

---

## Known limits

- **Learning unit is the capability, not the query.** No per-query difficulty estimate. If instances vary too much, `needs_split` says so; split the capability.
- **Self-reported confidence is one-directional.** Low confidence escalates; high confidence grants nothing. Verbalized confidence is biased toward overconfidence (see `docs/principles.md` section 5).
- **Gate 3 refuses rather than using a weaker judge.** Work from the top rung has no admissible cross-family judge and goes to a human.
- **Down-routing is riskier in the thick lane.** Multi-step failures compound; raise `floor_tier` there.
- **Thick lane has one model family.** `gate: full` is impossible there -- the linter catches it.
- **Expect the boring end of the savings range.** RouteLLM's 3.66x is measured against a random router at equal quality, not against always calling the strong model. Across 400K instances and 33 models, several published routers [fail to beat Best-Single-Model](https://arxiv.org/abs/2601.07206) (arXiv:2601.07206), and Random is competitive on math and code (arXiv:2504.07113). This workspace gates on verifiable evidence, so assume the low end. The reason to run it is that the run record answers the question for *your* workload.
- **No per-query difficulty estimation, deliberately.** 21 routing methods across 5 benchmarks converge far below oracle (arXiv:2606.07587). The unit of learning here is the capability -- category-level signal, stated as design rather than discovered as failure.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute, code conventions, testing, and the PR process.

## License

[MIT](LICENSE)
