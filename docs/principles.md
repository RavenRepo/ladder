# Principles

Why this workspace is shaped the way it is. Read this before the README; the
README tells you which commands to type, this tells you why the commands exist.

Nothing here is asserted on vibes. Every number is either measured on this
machine (and reproducible with `ladder probe`) or carries an arXiv id you can
go read. Where the literature is thin or contested, it says so.

---

## 1. The idea being borrowed

Graph engineering, as it arrived, is four claims:

1. A system improves between runs only if **something carries forward** and
   **something rejects work before it carries forward**. One without the other
   is either amnesia or drift.
2. Memory should have a **queryable shape**, not be a transcript. A graph, so
   you can ask it questions the transcript can't answer.
3. The thing that rejects bad work must sit **outside the agent doing the
   work**.
4. Once memory has a shape, the **launch becomes a query against it** rather
   than a fixed list — and that is the moment a static automation becomes a
   system that picks its own scope.

That is the whole method, and it transfers. What does *not* transfer unchanged
is the node. In a market graph a node is a company: it sits there waiting to be
described. Here the node has to be something that can be *done*, at a cost, by
one of several agents of different strength — and whether it was done well is
not a property of the node, it's a verdict someone has to reach.

So this workspace keeps two things the market version never had to: a record of
**what a rung of the ladder is capable of**, and a record of **how we found
out**.

---

## 2. What a node is here

**A node is a capability: a recurring class of work.**

Not a task, not a ticket, not a file. "Summarise a changelog into release
notes." "Triage an inbound source for relevance." "Write a database migration
from a schema diff." A capability is the unit because it is the thing that
*recurs* — and recurrence is what makes a routing policy learnable at all. A
one-off task teaches you nothing about the next one.

Each capability node carries the only thing worth remembering:

```
capability:  id · label · lane · risk_class
policy:      current_tier · pinned_by · since
evidence:    per-tier outcome counts, gate pass rates, sample size
```

Edge types:

```
escalates_to    this capability's failures climb to that tier
verified_by     this capability's output is gated by that verifier tier
requires        this capability cannot run until that one has
supersedes      this capability replaced that one
contradicts     two runs disagreed about what this capability should do
```

The graph's memory is `20-graph/outcomes.jsonl`: one append-only row per
dispatch, recording capability, tier, gate verdict, failure class, cost and
duration. That file is the whole learning substrate. Everything the system
knows about which model is good enough for what, it knows from there.

---

## 3. The measurement that reorganised this design

The brief was "orchestrate from higher-end models down to cheapest models."
Before building that, I measured what a dispatch actually costs here. Identical
prompt — `Reply with exactly: OK` — across surfaces:

| Surface | Model | Input tokens billed | Cost |
|---|---|---|---|
| kirocc HTTP | `claude-haiku-4.5` | **91** | subscription-backed |
| `claude -p` | `claude-haiku-4.5` | **22,810** (22,800 cache-create) | **$0.0468** |
| `claude -p` | `claude-opus-4.5` | **22,804** | **$0.2297** |

`claude -p` drags ~22.8k tokens of fixed harness into every dispatch —
`CLAUDE.md`, hooks, plugin tool schemas — and **it is identical on every
model**. Read the consequences carefully, because they are not the obvious
ones:

- Dropping opus → haiku *inside that harness* saves 4.9×. Real, but bounded.
- The **floor** for a one-word answer is $0.0468, and it is 100% overhead.
- The same call through kirocc uses **250× fewer input tokens**.

**The dominant cost term is not which model you pick. It is how much fixed
context the dispatch surface drags along.** A ladder that only orders models is
optimising the smaller term while the larger one sits untouched.

So the first decision for any capability is not *which tier* but **which lane**:

- **thin lane** — kirocc HTTP, ~91 tokens of overhead, no tools. Classification,
  routing decisions, extraction, scoring, verification, summarisation. Cheap
  tiers pay for themselves enormously here.
- **thick lane** — `claude -p`, ~22.8k tokens of overhead, full tools and repo
  context. Work that genuinely needs to read the filesystem and edit it. Going
  cheap here saves comparatively little, and the overhead is fixed, so what you
  give up is the only thing you were paying for.

Lane first, tier second. That ordering is `SCHEMA.md`'s first rule and it came
out of the table above, not out of a paper.

---

## 4. The substrate is a claim until you probe it

`pi` advertises 41 models. `omp` advertises 58. `opencode` advertises 7 free
ones. On probe, on this machine, today:

| Surface | Status |
|---|---|
| **kirocc** `127.0.0.1:3456` | **live** — 22 models, two families, no API key |
| **`claude -p`** | **live** |
| `codex exec` | 401, no credential |
| `pi --provider opencode-go` | 401 `CreditsError`, insufficient balance |
| `pi --provider kiro` | connection error — not pointed at the working proxy |
| `omp --model google-antigravity/…` | 429 rate limited |
| `opencode run` | no output in 120 s |

Most of the ladder anyone would have written down from the catalogs does not
exist. Worse, the dead surfaces fail in ways that *look like research
failures*: a 429 arrives as a non-zero exit, and a naive runner files it next
to work that failed on its merits, so the human queue fills with things no
human can act on.

Two consequences, both load-bearing:

1. **Availability is probed, never assumed** (`runner/substrate.py`), and the
   probe result is an artifact selection reads. "Dead" is a credential state,
   not a property — the weekly probe is what will notice when a card gets paid.
2. **Transport failure is a separate failure class from quality failure.** A
   timeout is retried *clean*; a gate rejection is retried *with the reason
   attached*. Telling an agent "your last attempt timed out" is noise that can
   only make its next answer worse.

---

## 5. Why the cheap tier cannot be trusted to say it's confident

The obvious design is: let the cheap model answer, have it report a confidence,
escalate when confidence is low. Three independent reasons that doesn't work
here.

**The good version of it needs logits, and we have none.** The strongest result
on cascade deferral (arXiv:2404.10136) shows sequence log-probability carries a
**length bias** that systematically mis-defers, and that what actually works is
a learned rule over *token-level* uncertainty features and model internals.
Every surface here is a black box — an HTTP endpoint or a subprocess. That
method is unavailable, not merely inconvenient.

**The other good version needs labelled data we don't have.** FrugalGPT
(arXiv:2305.05176) gets its headline — GPT-4 quality on HEADLINES for ~2% of
the spend — from a DistilBERT scorer trained on **labelled in-distribution
examples**. That headline is also the single best cell in the table, on three
short-form classification datasets, and the accuracy gain on that cell is ~1.5%,
not the "up to 4%" the abstract advertises. A fresh workspace has no labels.

**Self-reported confidence is worst exactly where it matters.** LLMs do not
reliably self-correct reasoning without external feedback — GPT-4 on GSM8K
degrades 95.5% → 91.5% → 89.0% across self-correction rounds, and GPT-3.5 on
CommonSenseQA collapses 75.8% → 38.1% (arXiv:2310.01798). Earlier results
claiming otherwise were using oracle labels to decide *when to stop*, which is
leaked ground truth.

So the deferral signal here is not the model's opinion of itself. **It is the
gate verdict**, recorded, counted, and learned from. The policy starts
pessimistic — a new capability runs at a tier you chose — and earns its way
down the ladder on evidence. That is slower than a trained router and it is the
only method the substrate actually licenses.

---

## 6. Why the verifier must not share a family with the generator

This is the single most important structural rule here, and it is the one with
the sharpest evidence behind it.

An evaluator's preference for its own output is **partly legitimate** — stronger
models rate their own work higher largely because it really is better. But the
harmful component is localised, and localised in the worst possible place: it
appears **precisely when the evaluator errs as a generator**, and stronger
models show *more* pronounced harmful self-preference on their own mistakes
(arXiv:2504.03846). Separately, models can identify their own outputs above
chance, and self-recognition ability correlates causally with self-preference
strength (arXiv:2404.13076).

Put plainly: **self-judgment fails in exactly the case a gate exists to catch.**
A model is worst at catching the errors it is most likely to make.

This machine makes the fix nearly free. kirocc serves **two families — Claude
and GPT-5.6 — from one localhost endpoint**, at 744–784 ms for the cheap rungs.
So `counter_family()` in `runner/tiers.py` is a hard constraint, not a
preference: work generated on a Claude rung is gated on a GPT rung and vice
versa. The literature says it is mandatory; the substrate says it costs almost
nothing. They rarely agree that cleanly.

**Can a *cheap* verifier gate an *expensive* generator?** Partly, and the split
is what the gate ladder encodes. Verification is easier than generation for
**mechanically checkable** claims — does every assertion carry an evidence line,
does the cited file exist, does the diff touch the module it says it touched,
do the tests pass. It is *not* easier for **judgment** — is this reasoning
sound, is this the right trade-off. So cheap rungs gate the checkable half and
judgment escalates to a peer-or-stronger rung of the other family. The relevant
formalisation is the generation-verification gap (arXiv:2412.02674), which also
reports that self-improvement by self-filtering **saturates** — a caution
against believing the loop will keep paying forever.

---

## 7. Do not build a debate

Multi-agent debate is the intuitive way to spend more compute on a hard
problem, and the evidence for it is worse than its popularity suggests. At
matched budget, plain self-consistency over 6 samples beat multi-agent debate
over 6 (85.3% vs 83.2% on GSM8K, arXiv:2310.01798) — the gains attributed to
debate structure look mostly like the gains from sampling.

The topology-optimisation literature points the same way when you read what it
actually measured. MaAS (arXiv:2502.04180) samples a **query-dependent**
sub-architecture instead of running one fixed topology, and reports **6–45% of
the inference cost** of handcrafted multi-agent systems at **+0.54% to +11.82%**
accuracy — the lower bound of that accuracy band is noise, so the durable result
is the *cost* result: **most queries don't need the full topology.** That is
step 10 of the source method ("route by node state") arrived at independently
and with numbers.

DyLAN (arXiv:2310.02170) reports +13.0% MATH and +13.3% HumanEval from pruning
the agent team, but against a *single* GPT-3.5 call rather than matched compute,
and its peer-rating importance score is circular when every agent shares a
backbone. GPTSwarm (arXiv:2402.16823) — agents as an optimizable graph, with
both node and edge optimisation — is the cleanest statement of the idea, but its
edge optimiser needs many scored rollouts per topology, on short-horizon
benchmarks. AFlow (arXiv:2410.10762) gets smaller models to beat larger ones by
searching workflows with MCTS, and requires **an executable scorer per task** —
which most real engineering work does not have.

So: no debate, no learned topology search, not yet. **Sampling plus an external
gate**, and a routing policy learned from gate verdicts. When a capability
acquires a real executable scorer — a test suite — it becomes eligible for more
aggressive automation, and the schema records which capabilities those are.

---

## 8. Where the money actually is

Not in the model price. In **not doing settled work twice**.

Every serious result in this area reduces to the same economics: skip what is
already verified, delta-check what is merely stale, and spend the expensive rung
only on what is new or contradicted. MaAS's 6–45% cost figure is that. The
source method's step 10 is that. The second run of any graph being a fraction of
the first is that.

`escalation_path()` is deliberately **monotone in capability** for the same
reason: a failed rung may never escalate sideways into a rung of equal rank.
Otherwise the second attempt is a re-roll wearing a correction's clothes, and
the run record shows two failures where there was really one coin flipped twice.
Escalation *may* cross family, and that is intentional — cascade gains are
limited by **error correlation across tiers** (arXiv:2502.09054, which buys
−13.0% cost by letting cheap stages abstain for the whole cascade precisely
because a query the small model finds hopeless is often one the frontier model
also fails). Crossing family is the cheapest available decorrelation.

---

## 9. The meta-loop proposes; it never writes

Once a week, one agent with fresh context reads `40-runs/`, finds failure
reasons that repeat, and proposes diffs to `SKILL.md`, `SCHEMA.md`,
`CONSTRAINTS.md` and the tier policy. **It returns a diff. A human applies it.**

This is the one rule the source method is emphatic about and it is right, and
the literature gives it teeth. ADAS (arXiv:2408.08435) searches over agent
programs scored on benchmark performance — direct Goodhart exposure. Self-
improvement by self-filtering saturates (arXiv:2412.02674). An agent that can
edit its own constraints will eventually edit away the inconvenient one, and it
will have a good explanation ready.

The automatic half is narrower and safer: **tier policy moves on counted gate
outcomes**, under a rule written in `SCHEMA.md`, with a minimum sample size and
a hysteresis band so a policy doesn't oscillate on noise. Numbers move the
policy. Only a human moves the rules that decide what the numbers mean.

---

## 10. What this does not fix

It does not fix a bad capability definition, and no amount of graph structure
will. It does not decide which trade-off matters or what the work should be.
It will not make a cheap model good at something it is bad at — it will only
find out, cheaply and on the record, and stop routing there.

And the honest limit on the whole premise: the routing literature's headline
numbers are softer than they look. RouteLLM's celebrated 3.66× (arXiv:2406.18665)
is measured **against a random router at equal quality**, not against always
calling the strong model, and the same router delivers 1.41× on MMLU and 1.49×
on GSM8K. Expect the boring end of that range. The reason to build this is not
a 98% saving; it is that the run record makes the question *answerable on your
own workload*, which no paper can do for you.
