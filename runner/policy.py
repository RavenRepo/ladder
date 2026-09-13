"""The routing policy: which rung a capability starts on, and how that moves.

This is the part that makes the workspace improve between runs rather than
merely repeat. Everything it knows comes from `20-graph/outcomes.jsonl` — one
append-only row per dispatch, written by the runner, never edited by hand.

The shape of the problem
------------------------
We cannot use the methods the cascade literature actually validated. Token-level
uncertainty deferral (arXiv:2404.10136) needs logits, and every surface here is
a black box. A trained scorer (arXiv:2305.05176) needs labelled in-distribution
data, which a fresh workspace does not have. Self-reported confidence is
unusable because models are worst at judging exactly the outputs they got wrong
(arXiv:2310.01798, arXiv:2504.03846).

What is left is the one signal this workspace genuinely owns: **did the gate
pass**. So the policy is an explore/exploit loop over gate verdicts, and it is
deliberately unclever:

  - start PESSIMISTIC. A new capability runs at the tier its author declared.
    Never at the cheapest rung — an unproven capability failing on a cheap rung
    teaches you nothing about the capability, only about the rung.
  - EXPLORE downward on a small fraction of dispatches. One rung cheaper, same
    gate, result recorded. This is the only way evidence for a cheaper rung can
    ever exist, because a rung you never route to is a rung you never learn.
  - PROMOTE the cheaper rung to current once it has enough trials above the
    bar.
  - DEMOTE back up if the current rung falls below the bar minus a hysteresis
    band, so a policy does not oscillate on noise.

The asymmetry is deliberate: promotion (getting cheaper) requires a minimum
sample size, demotion (getting safer) does not. Being wrong about "this cheap
rung is fine" costs quality on every future run; being wrong about "go back up"
costs money on a few.

What this is not
----------------
Not a contextual bandit with regret bounds. Not a learned router. There is no
per-query difficulty estimate at all — the unit of learning is the CAPABILITY,
not the query, because capabilities recur and queries do not. If a capability's
instances vary so much that one tier cannot serve them, that is a signal the
capability is defined too broadly, and `needs_split()` reports it.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import json
import pathlib
import random

from .tiers import BY_NAME, climb_order, escalation_path

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTCOMES = ROOT / "20-graph" / "outcomes.jsonl"

# --- the constants the policy turns on ------------------------------------
# These live here rather than in a config file because changing them changes
# what "verified" means, and that should show up in a diff and a code review.
# SCHEMA.md documents them; this is the implementation of that document.

MIN_SAMPLES = 12        # trials on a rung before it may become current
PROMOTE_AT = 0.90       # gate pass rate a cheaper rung must clear to be adopted
DEMOTE_BELOW = 0.75     # current rung falling below this goes back up at once
EXPLORE_RATE = 0.15     # fraction of dispatches that trial one rung cheaper
SPLIT_SPREAD = 0.35     # pass-rate spread across instances that means "too broad"


@dataclasses.dataclass
class Outcome:
    """One dispatch, as recorded. The atom of everything below."""

    capability: str
    tier: str
    verdict: str              # pass | fail | transport | malformed
    gate_stage: str           # script | checkable | judgment | pass
    reason: str = ""
    cost_usd: float = 0.0
    seconds: float = 0.0
    run_id: str = ""
    instance: str = ""        # optional: which concrete item, for needs_split
    stamp: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "Outcome":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in known})


def record(outcome: Outcome, path: pathlib.Path = OUTCOMES) -> None:
    """Append one outcome. Append-only, because this file is the audit trail.

    Anything that spends a dispatch must land here — including malformed
    replies, which are neither retried nor escalated and would otherwise be
    invisible. A run record that shows a clean stop while dispatches went
    unaccounted for is worse than no record.
    """
    if not outcome.stamp:
        outcome.stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dataclasses.asdict(outcome), sort_keys=True) + "\n")


def load(path: pathlib.Path = OUTCOMES) -> list[Outcome]:
    if not path.exists():
        return []
    outcomes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            outcomes.append(Outcome.from_row(json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            # A corrupt row is skipped, not fatal. Losing one row degrades the
            # policy slightly; refusing to start loses the whole history.
            continue
    return outcomes


# --- counting -------------------------------------------------------------

@dataclasses.dataclass
class Stats:
    """Gate performance of one (capability, tier) pair."""

    trials: int = 0
    passes: int = 0
    transport: int = 0      # excluded from the rate: not evidence about quality

    @property
    def rate(self) -> float:
        return self.passes / self.trials if self.trials else 0.0

    @property
    def decided(self) -> bool:
        return self.trials >= MIN_SAMPLES


def tally(outcomes: list[Outcome]) -> dict[tuple[str, str], Stats]:
    """Gate pass rate per (capability, tier).

    Transport failures are counted separately and kept OUT of the rate. A rate
    limit is not evidence that a model is bad at a task, and letting it
    depress a pass rate would push the policy up the ladder for a reason that
    has nothing to do with capability — expensively, and for as long as the
    outage lasts.

    Malformed replies DO count against the rate. A model that cannot hold the
    return schema is genuinely unfit for the rung, and that is exactly the kind
    of thing a cheap rung fails at.
    """
    stats: dict[tuple[str, str], Stats] = collections.defaultdict(Stats)
    for outcome in outcomes:
        cell = stats[(outcome.capability, outcome.tier)]
        if outcome.verdict == "transport":
            cell.transport += 1
            continue
        cell.trials += 1
        if outcome.verdict == "pass":
            cell.passes += 1
    return dict(stats)


# --- the decision ---------------------------------------------------------

@dataclasses.dataclass
class Decision:
    tier: str
    why: str
    exploring: bool = False


def choose(capability: str, *, lane: str, floor_tier: str,
           outcomes: list[Outcome] | None = None,
           weights: dict | None = None,
           rng: random.Random | None = None,
           available: set[str] | None = None) -> Decision:
    """Pick the rung to dispatch this capability on.

    `floor_tier` is the author's declared starting point and the policy's
    ceiling of pessimism: the policy may move *cheaper* than it on evidence,
    and it may escalate above it on failure, but a capability with no history
    runs there.

    `available` is the set of surfaces the probe found alive. A rung whose
    surface is dead is skipped rather than dispatched into — that is the whole
    reason substrate health is an artifact and not an assumption.
    """
    rng = rng or random.Random()
    stats = tally(outcomes if outcomes is not None else load())
    rungs = [t for t in climb_order(lane, weights=weights)
             if available is None or t.surface in available]
    if not rungs:
        return Decision(floor_tier, "no rung in this lane has a live surface", False)

    current = _settled_tier(capability, floor_tier, stats, rungs)

    # Demote to safety first: a failing current rung is not a candidate for
    # further exploration downward.
    here = stats.get((capability, current))
    if here and here.decided and here.rate < DEMOTE_BELOW:
        upward = [t for t in escalation_path(current, weights=weights)
                  if available is None or t.surface in available]
        if upward:
            return Decision(
                upward[0].name,
                f"{current} fell to {here.rate:.0%} over {here.trials} trials "
                f"(below {DEMOTE_BELOW:.0%}); climbing",
            )

    # Explore one rung cheaper, sometimes. This is the only source of evidence
    # for a rung the policy is not already using.
    cheaper = _next_cheaper(current, rungs)
    if cheaper and rng.random() < EXPLORE_RATE:
        trial = stats.get((capability, cheaper.name), Stats())
        if not trial.decided or trial.rate >= PROMOTE_AT:
            return Decision(
                cheaper.name,
                f"exploring one rung cheaper ({trial.trials}/{MIN_SAMPLES} trials so far)",
                exploring=True,
            )

    return Decision(current, _why_current(capability, current, floor_tier, stats))


def _settled_tier(capability: str, floor_tier: str,
                  stats: dict[tuple[str, str], Stats],
                  rungs: list) -> str:
    """The cheapest rung that has earned its place, else the declared floor.

    Walks cheapest-first and takes the first rung with enough trials above the
    promotion bar. Rungs with too little evidence are skipped, not assumed bad
    — they are what exploration is for.
    """
    for tier in rungs:
        cell = stats.get((capability, tier.name))
        if cell and cell.decided and cell.rate >= PROMOTE_AT:
            return tier.name
    return floor_tier


def _next_cheaper(current: str, rungs: list):
    names = [t.name for t in rungs]
    if current not in names:
        return None
    index = names.index(current)
    return None if index == 0 else rungs[index - 1]


def _why_current(capability: str, tier: str, floor_tier: str,
                 stats: dict[tuple[str, str], Stats]) -> str:
    cell = stats.get((capability, tier))
    if tier == floor_tier and not cell:
        return "no history; running at the declared floor tier"
    if not cell:
        return "no history at this rung"
    if cell.decided:
        return f"{cell.rate:.0%} gate pass over {cell.trials} trials"
    return f"only {cell.trials}/{MIN_SAMPLES} trials; holding"


# --- the thing that tells you the capability is wrong ---------------------

def needs_split(capability: str, outcomes: list[Outcome] | None = None) -> dict | None:
    """Report a capability whose instances disagree too much to share a tier.

    The policy's unit of learning is the capability. That only works if the
    capability's instances are alike. When the per-instance pass rate on one
    rung spreads wider than SPLIT_SPREAD, no single tier assignment is right
    and the honest fix is upstream — split the capability — not a cleverer
    router.

    This returns a finding for a human. It never splits anything itself.
    """
    outcomes = outcomes if outcomes is not None else load()
    rows = [o for o in outcomes
            if o.capability == capability and o.instance and o.verdict != "transport"]
    if len(rows) < MIN_SAMPLES:
        return None

    per_tier: dict[str, dict[str, list[bool]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for row in rows:
        per_tier[row.tier][row.instance].append(row.verdict == "pass")

    for tier, instances in per_tier.items():
        # Only instances with repeat observations say anything about spread.
        rates = [sum(v) / len(v) for v in instances.values() if len(v) >= 3]
        if len(rates) < 3:
            continue
        spread = max(rates) - min(rates)
        if spread >= SPLIT_SPREAD:
            return {
                "capability": capability,
                "tier": tier,
                "spread": round(spread, 2),
                "instances": len(rates),
                "finding": (
                    f"instances of {capability!r} disagree by {spread:.0%} on {tier}. "
                    "One tier assignment cannot serve them. Split the capability."
                ),
            }
    return None


def report(outcomes: list[Outcome] | None = None) -> list[dict]:
    """Current policy state, per capability. What `ladder policy` prints."""
    outcomes = outcomes if outcomes is not None else load()
    stats = tally(outcomes)
    capabilities = sorted({c for c, _ in stats})
    rows = []
    for capability in capabilities:
        cells = [(tier, cell) for (cap, tier), cell in stats.items() if cap == capability]
        cells.sort(key=lambda pair: BY_NAME[pair[0]].rank if pair[0] in BY_NAME else 99)
        rows.append({
            "capability": capability,
            "tiers": [
                {"tier": tier, "trials": cell.trials, "rate": round(cell.rate, 3),
                 "transport": cell.transport, "decided": cell.decided}
                for tier, cell in cells
            ],
            "split": needs_split(capability, outcomes),
        })
    return rows
