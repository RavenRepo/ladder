# SCHEMA.md

What a node is, what an edge is, what a tier is, and the thresholds that decide
when the system is allowed to believe something. One line here determines every
question the graph can answer later, so it is written down before anything runs.

Only the meta-loop proposes edits to this file, and it proposes a **diff**.
A human applies it. See `templates/meta-review.md`.

---

## Rule 0 — lane before tier

Every capability is assigned a **lane** before it is assigned a tier, because
the lane is the larger cost term and the tier is the smaller one.

| Lane | Surface | Fixed overhead | What belongs here |
|---|---|---|---|
| `thin` | kirocc HTTP | ~91 input tokens | classification, extraction, scoring, routing decisions, verification, summarisation |
| `thick` | `claude -p` | ~22,800 input tokens | anything that must read or edit the filesystem, or hold repo context |

Measured, not estimated — reproduce with `ladder probe --measure-weight`.
A one-word answer costs **$0.0468** on the cheapest `thick` rung and **$0.2297**
on the most expensive, and 100% of the cheap figure is harness. Putting a
classification job in the `thick` lane is the most expensive mistake available
here, and no choice of model repairs it.

**A capability may not change lane without a human.** Moving lanes changes what
the capability can observe, not just what it costs.

---

## Node types

```
capability   a recurring class of work. THE primary node type.
tier         a (surface, model) pair. Defined in runner/tiers.py, not here.
gate         a check that can reject a return. Four of them, see below.
constraint   a correction that carries forward. Lives in CONSTRAINTS.md.
```

A capability node:

```
id            stable slug, never reused
label         one line, in the imperative: "triage an inbound source"
lane          thin | thick
risk_class    reversible | costly | irreversible
floor_tier    the tier an unproven instance runs at. Pessimistic by default.
scorer        none | tests | script   — whether an executable check exists
```

`risk_class` is not decoration. `irreversible` capabilities — anything that
sends, pays, publishes, or writes outside this workspace — are **excluded from
downward exploration entirely**. The policy may never trial a cheaper rung on
work that cannot be taken back. It runs at `floor_tier` or above, always.

`scorer` records whether the capability has a real executable check. Workflow
search gets its results from tasks that have one (arXiv:2410.10762), and most
engineering work does not. Capabilities with `scorer: tests` are eligible for
more aggressive automation; capabilities with `scorer: none` are not, and
saying so in the schema stops that distinction from being quietly forgotten.

---

## Edge types

```
escalates_to   failures on this capability climb to that tier
verified_by    output of this capability is gated by that tier
requires       this capability cannot run until that one has
supersedes     this capability replaced that one
contradicts    two runs disagreed about what this capability should do
```

Every edge carries an evidence field naming the run and row it came from. An
edge with no evidence line is not an edge; it is an opinion, and it is rejected
at gate 1.

---

## The tier ladder

Defined in `runner/tiers.py` because it is executable and must stay in sync
with the probe. Summary of the invariants it enforces:

- A **tier is a (surface, model) pair**, never a model alone. The same model
  through two surfaces is two rungs with a 250× difference in fixed cost.
- **Cost is three declared currencies** — `usd`, `tokens_in`, `seconds` — and a
  launch supplies the weights. kirocc meters no dollars; weighting dollars only
  on a kirocc launch makes every rung score zero and the ladder meaningless.
- **Escalation is monotone in capability.** A failed rung may never escalate
  sideways to a rung of equal rank. Otherwise the retry is a re-roll wearing a
  correction's clothes.
- **Escalation may cross family, deliberately.** Cascade gains are bounded by
  error correlation across tiers (arXiv:2502.09054); crossing family is the
  cheapest decorrelation available.

---

## The gate — four stages, cheapest first

The script is free, so it rejects everything it can before a token is spent.

| # | Gate | Catches | Who runs it |
|---|---|---|---|
| 1 | **script** | schema breaks, missing fields, dead links, missing evidence lines, non-canonical labels | deterministic code, no model |
| 2 | **checkable** | claims that can be mechanically confirmed: cited file exists, diff touches the named module, tests pass, evidence line supports the assertion | a **cheap rung of the other family** |
| 3 | **judgment** | is the reasoning sound, is this the right approach, is the evidence thin | a **peer-or-stronger rung of the other family** |
| 4 | **human** | whatever failed twice | `30-queries/needs-human.md`, your five minutes a day |

### The two rules the gate exists to enforce

**A verifier may not share a model family with the generator.** Not a
preference — a hard constraint in `counter_family()`. An evaluator's harmful
self-preference appears precisely when it errs as a generator, and stronger
models show it *more* on their own mistakes (arXiv:2504.03846); self-recognition
drives self-preference causally (arXiv:2404.13076). Self-judgment fails in
exactly the case a gate exists to catch.

**A cheap verifier gates checkable claims, never judgment.** Verification is
easier than generation for mechanically confirmable facts, and not easier for
taste. That is the line between gate 2 and gate 3, and it is why gate 3 requires
a peer-or-stronger rung. The governing formalisation is the generation-
verification gap (arXiv:2412.02674), which also reports that self-improvement by
self-filtering **saturates** — do not expect this loop to keep paying forever.

**Self-reported confidence is not admissible at any gate.** Not as a threshold,
not as a tiebreak. Models do not reliably self-correct without external feedback
(arXiv:2310.01798), and the confidence-based deferral that does work needs
token-level uncertainty and model internals (arXiv:2404.10136) which no surface
here exposes. A return may *carry* a confidence field for the human queue to
read; nothing automated may branch on it.

---

## Failure classes — three, and they go to different places

Lumping them together produces a human queue full of things no human can act on,
and a human queue that is not actionable does not get read.

| Class | What it is | What happens |
|---|---|---|
| `transport` | the agent never answered: timeout, non-zero exit, 401, 429, CLI missing | retried **without** a reason — there is nothing to correct. Kept **out of the pass-rate tally**. Never goes to the human queue. |
| `malformed` | it answered, but not in the schema: prose, an apology, no JSON | logged, dropped, not retried. **Counts against the rate** — a model that cannot hold the schema is unfit for the rung. |
| `quality` | a real return that fails shape, threshold, or a verifier | retried **with the reason appended**, escalated one rung, and to `needs-human.md` at the retry cap. Counts against the rate. |

The retry asymmetry is the point. Passing the reason back turns a retry into a
correction. Telling an agent "your last attempt timed out" is noise that can
only make its next answer worse.

Keeping `transport` out of the tally matters more than it looks: a rate-limit
outage that depressed a pass rate would push the policy **up** the ladder, for a
reason having nothing to do with capability, for as long as the outage lasted.

---

## Thresholds

Implemented in `runner/policy.py`. Changing them changes what "good enough"
means, so they live in code where a diff shows up in review.

```
MIN_SAMPLES    12     trials on a rung before it may become current
PROMOTE_AT     0.90   gate pass rate a cheaper rung must clear to be adopted
DEMOTE_BELOW   0.75   current rung falling below this climbs immediately
EXPLORE_RATE   0.15   fraction of dispatches that trial one rung cheaper
SPLIT_SPREAD   0.35   per-instance pass-rate spread that means "too broad"
```

The asymmetry is deliberate: **promotion requires a sample size, demotion does
not.** Being wrong about "this cheap rung is fine" costs quality on every future
run. Being wrong about "go back up" costs money on a few.

`SPLIT_SPREAD` is the schema noticing its own unit is wrong. The policy learns
per *capability*, which only works if a capability's instances are alike. When
they spread wider than this, no tier assignment is right and the fix is
upstream — split the capability. `needs_split()` reports it to a human and
splits nothing itself.

---

## Verified

```
A capability's tier assignment counts VERIFIED when:
  - >= MIN_SAMPLES non-transport trials on that rung, AND
  - gate pass rate >= PROMOTE_AT, AND
  - at least one of those passes cleared gate 3 (judgment), not only gates 1-2
```

The third clause is what stops a capability from being certified by the cheap
half of the gate alone. A rung that has only ever been checked for schema
conformance and file existence has not been checked for being right.
