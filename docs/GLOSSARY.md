# Glossary

Every term this workspace uses that doesn't mean what you'd guess. Read this
once; it takes four minutes and saves an hour.

Terms are ordered so that each one only uses words defined above it.

---

## The core five

### Surface
**A way to reach a model.** Not the model itself — the *road* to it.

The default configuration ships with two surfaces:

| Surface | What it is | Has tools? |
|---|---|---|
| `kirocc` | a local OpenAI-compatible HTTP proxy at `127.0.0.1:3456` | no |
| `claude-cli` | running `claude -p` as a subprocess | yes |

`kirocc` is the name used in this project for the thin-lane HTTP endpoint. You
can substitute any OpenAI-compatible endpoint (Ollama, vLLM, LiteLLM, or any
proxy that serves `/v1/models` and `/v1/messages`) by changing `KIROCC_BASE` in
`runner/substrate.py`.

Why it matters: reaching `haiku-4.5` through `kirocc` costs **91 input tokens**.
Reaching *the same model* through `claude-cli` costs **22,810**, because the
CLI loads CLAUDE.md, hooks and every tool schema first. The surface, not the
model, is where most of your money goes.

---

### Lane
**How much fixed baggage a surface carries.** There are exactly two.

- **`thin`** — ~91 tokens of overhead. No tools, no filesystem. Good for
  thinking about text you hand it: classifying, extracting, scoring, checking.
- **`thick`** — ~22,800 tokens of overhead. Full tools and repo access. Needed
  when the work must actually *read or edit files*.

> **The one rule to remember:** pick the lane before you pick the model.
> Lane is a 250× decision. Model is a 5× decision.

---

### Tier (a "rung")
**A (surface, model) pair.** One step on the ladder.

`haiku-4.5` and `cli-haiku-4.5` are *the same model* and *two different tiers*,
because they sit on different surfaces with a 250× cost gap. Calling both of
them "haiku" would hide the only number that matters.

Every tier has:
- a **rank** — `0` is strongest. Lower rank = more capable.
- a **family** — `claude` or `gpt`. This matters enormously; see *Gate*.
- a **cost** — three separate numbers, see below.

---

### Capability
**A recurring class of work — and the thing this system learns about.**

Good capabilities (they *recur*, so you can learn from repetition):
- "triage an inbound source for relevance"
- "summarise a changelog into release notes"
- "classify why a gate failed"

Not capabilities (one-offs teach you nothing about the next one):
- "fix the bug in auth.py"
- "review this specific PR"

> Capability is the **unit of learning**. The system does not ask "is *this
> query* hard?" — nobody can answer that reliably yet. It asks "has
> *this kind of work* succeeded on the cheap rung enough times to trust it?"

---

### Instance
**One concrete item a capability is applied to.**

If the capability is "triage a source", an instance is one specific URL.
A launch supplies a list of instances; each one becomes one dispatch.

---

## How work flows

### Dispatch
**One unit of work sent to one tier.** One instance → one model call.
Everything that costs money or quota is a dispatch, and every dispatch is
recorded — even the ones that come back broken.

### Return
**What came back from a dispatch.** Either a valid JSON record, or nothing
usable.

### Claim
**One assertion inside a return, with its evidence.**

```json
{"statement": "this source is first-party", "evidence": "https://example.com/about"}
```

A claim without evidence is not a claim. It gets rejected for free.

### Evidence
**Proof attached to a claim.** Exactly two kinds are accepted:

| Kind | Looks like | How it's checked |
|---|---|---|
| **locator** | `https://…`, `src/a.py#L42`, `src/a.py:17` | shape now, existence at gate 2 |
| **quotation** | verbatim text from what the prompt supplied | matched against the source text, **free** |

A quotation that isn't actually in the source is a **fabricated citation** and
is caught by a regex — no model call needed.

---

## The gate

### Gate
**The thing that decides whether a return is acceptable.** It runs *outside*
the agent that produced the work — always, no exceptions.

Why: a model rereading its own output sees every reason it wrote things that
way, so it approves. Worse, that blind spot is **strongest exactly where the
model made a mistake**. Self-review fails precisely in the case a gate exists
to catch.

Four stages, cheapest first:

| # | Stage | What it checks | Cost |
|---|---|---|---|
| 1 | `script` | shape, evidence present and locatable, no placeholder markers | **free** |
| 2 | `checkable` | does the cited evidence exist and support the claim? | one cheap call |
| 3 | `judgment` | is this actually *right*? | one strong call |
| 4 | `human` | whatever failed twice | your five minutes |

### Cross-family rule
**A verifier may never be the same model family as the generator.**

Claude work is checked by GPT. GPT work is checked by Claude. This is a hard
constraint in the code, not a preference — a model is worst at spotting exactly
the kind of error it makes itself.

### Gate 3 refusal
If the work came from the **strongest rung available** and there's no
peer-or-stronger rung of the *other* family, gate 3 **refuses** rather than
using a weaker judge.

A weak judge doesn't give you a weak gate. It gives you a gate that waves
through exactly what it was installed to catch — while reporting success. So
the record goes to a human instead.

---

## Outcomes: the four things that can happen

This distinction is the most important one in the codebase. They go to
**different places** because they mean different things.

| Class | What happened | Retried? | Counts against the tier? | Human queue? |
|---|---|---|---|---|
| `pass` | cleared every stage | — | counts as success | no |
| `quality` | real answer, failed a gate | yes, **with the reason**, one rung up | **yes** | after 2 attempts |
| `transport` | never answered (timeout, 401, 429, 502) | yes, **clean** | **no** | never |
| `malformed` | answered, but not in the schema | no | **yes** | no |
| `deferred` | passed, but said it wasn't confident | escalates a rung, **free** | **no** | only at the top |

Three things to internalise:

1. **`transport` is excluded from scoring.** A rate limit says nothing about
   whether a model is good at a task. If outages counted as failures, an
   afternoon of 429s would push your routing to the most expensive model and
   keep it there.
2. **`quality` retries carry the reason; `transport` retries don't.** Telling
   an agent *why* it failed turns a retry into a correction. Telling it "you
   timed out" is noise that can only make the next answer worse.
3. **`malformed` is never retried.** There's no correction to hand back — the
   reply was never a record in the first place.

---

## The learning parts

### Floor tier
**Where a capability starts when nothing is known about it.**

Deliberately pessimistic. Never the cheapest rung — a brand-new capability
failing on the cheapest model teaches you about *the model*, not about the
capability.

### Exploration
**Occasionally trying a cheaper rung on purpose** (15% of dispatches).

This is the only way evidence for a cheaper rung can ever exist. A rung you
never route to is a rung you never learn about.

Exploration aims at the **cheapest undecided rung**, not the next one down —
otherwise reaching the bottom of a 7-rung ladder would take ~560 dispatches.

### Promotion / demotion
- **Promotion** (getting cheaper) requires **12+ trials at ≥90% pass rate**.
- **Demotion** (getting safer) happens immediately below 75%.

The asymmetry is deliberate: being wrong about "this cheap rung is fine" costs
quality on every future run. Being wrong about "go back up" costs money on a
few.

### `needs_split`
**A warning that a capability is defined too broadly.**

If different instances of the same capability succeed and fail wildly
differently on the same rung, no single tier assignment can serve them. The
fix is upstream — split the capability in two. No routing cleverness repairs a
bad category.

---

## The files

### Launch
**A markdown file describing one sweep of work.** Lives in `00-launches/`.

Critically, a launch does **not** name a model. It names a capability and
supplies instances; *the policy* decides the rung from history. That's what
makes it a workflow rather than a script — the same file dispatches somewhere
else next month without anyone editing it.

### `outcomes.jsonl`
**Append-only. One row per dispatch. The entire learning substrate.**

Everything the system believes about which model is good enough for what, it
believes because of this file. It is gitignored on purpose: a fresh clone must
start with an empty history, because inheriting someone else's outcomes means
believing things about *their* workload.

### `needs-human.md`
**The only file a human writes decisions into.** The system appends; nothing
ever rewrites it.

It is kept small on purpose. Only twice-failed *quality* failures reach it,
because a queue containing timeouts is a log, and logs don't get read.

### Meta-loop
**A weekly review agent that proposes changes to the rules.**

It reads the run records, finds failure reasons that repeat, and **returns a
diff**. A human applies it. It never writes to `SKILL.md`, `SCHEMA.md` or
`CONSTRAINTS.md` itself.

Why so strict: in a published self-improving-agent experiment, the agent
fabricated its tool-use logs *and deleted the markers used to detect its own
hallucinations*. A system allowed to edit its own instrumentation will
eventually edit the instrumentation instead of the behaviour — and have a good
explanation ready.

---

## Cost: three numbers, not one

`Cost` is never a single figure here, because three different currencies are in
play:

| Field | Unit | Applies to |
|---|---|---|
| `usd` | metered dollars | `claude-cli` only |
| `tokens_in` | fixed context before your prompt | both |
| `seconds` | observed latency | both |

`kirocc` costs **$0.00** — it's subscription-backed. So if you optimise for
dollars on a kirocc launch, **every rung scores zero** and the ladder becomes
meaningless. Use `optimise: seconds` there.

This is why launches declare `optimise:` explicitly instead of the code
guessing.
