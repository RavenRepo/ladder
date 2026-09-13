"""The ladder itself: tiers, what they cost, and the order you climb them.

A tier is not a model. A tier is a (surface, model) pair, because the same
model costs two wildly different amounts depending on which surface you reach
it through — 91 input tokens via kirocc, 22,810 via `claude -p`. Treating
"haiku" as one rung hides the only number that matters.

Cost is not a scalar, and pretending it is produces confident nonsense
------------------------------------------------------------------------
Three different currencies are in play on this machine:

  dollars   `claude -p` is metered. A dispatch costs $0.047 (haiku) to
            $0.23 (opus) before the task text is even considered.
  quota     kirocc is subscription-backed. A dispatch costs no dollars and
            some unknown fraction of a rate limit you will discover by
            hitting it.
  seconds   every dispatch costs wall clock, and on a 300-agent sweep the
            critical path is the thing you actually feel.

So `Tier.cost` is three declared numbers with declared units, and
`objective()` combines them with weights the launch file sets. A launch that
cares about a monthly bill and a launch that cares about finishing before
standup are not the same query, and neither should be hard-coded.

What the literature does and does not license here
--------------------------------------------------
The well-studied way to decide "can the cheap tier take this?" is token-level
uncertainty (arXiv:2404.10136), and it needs logits. Every surface here is a
black box — an HTTP endpoint or a subprocess — so that method is unavailable,
not merely inconvenient. FrugalGPT's learned scorer (arXiv:2305.05176) needs
labeled in-distribution data, which a fresh workspace does not have either.

What is left is the thing this workspace can actually observe: whether the
gate passed. So the routing policy is empirical and local — for this
capability, the cheapest tier whose gate pass rate cleared the bar over enough
runs — and it starts pessimistic and earns its way down. See `policy.py`.
"""

from __future__ import annotations

import dataclasses

from .substrate import Lane


@dataclasses.dataclass(frozen=True)
class Cost:
    """Three currencies, declared. None of them is "cost"."""

    usd: float          # metered dollars per dispatch, at zero task tokens
    tokens_in: int      # fixed context dragged along before your prompt
    seconds: float      # observed median latency for a short answer

    def objective(self, w_usd: float = 1.0, w_tokens: float = 0.0,
                  w_seconds: float = 0.0) -> float:
        """Scalarise, under weights the caller owns.

        Default weights optimise dollars only, which is right for a metered
        surface and actively wrong for a subscription-backed one — a launch
        against kirocc should weight seconds, because dollars are all zero
        there and an all-zero objective makes every rung look identical.
        """
        return w_usd * self.usd + w_tokens * self.tokens_in / 1000.0 + w_seconds * self.seconds


@dataclasses.dataclass(frozen=True)
class Tier:
    """One rung: a model reached through a specific surface."""

    name: str            # what a launch file and an outcome record refer to
    surface: str         # must match a Surface.name from substrate.py
    model: str           # the id the surface expects, verbatim
    lane: str            # thin | thick — inherited from the surface
    family: str          # claude | gpt — the axis a verifier must differ on
    rank: int            # 0 = strongest. Climb order, not quality proof.
    cost: Cost
    note: str = ""


# --- the ladder, as measured on this machine ------------------------------
# Latencies are from a cold "Say OK" probe, max_tokens=8. They are indicative,
# not a benchmark: they measure time-to-first-useful-response for a trivial
# prompt, which is what a router needs and not what a user experiences.
#
# usd=0.0 on kirocc rungs is literal — the proxy is backed by a subscription
# credential and meters nothing. It is NOT a claim that these calls are free of
# consequence; they spend quota. Weight seconds when you route within kirocc.

LADDER: tuple[Tier, ...] = (
    # --- thin lane · kirocc · Claude family -------------------------------
    Tier("opus-5", "kirocc", "claude-opus-5[1m]", Lane.THIN, "claude", 0,
         Cost(0.0, 91, 1.50),
         "1M context. The rung you escalate to, not the one you start on."),
    Tier("opus-4.5", "kirocc", "claude-opus-4.5", Lane.THIN, "claude", 1,
         Cost(0.0, 91, 1.57)),
    Tier("sonnet-5", "kirocc", "claude-sonnet-5", Lane.THIN, "claude", 2,
         Cost(0.0, 91, 1.69)),
    Tier("haiku-4.5", "kirocc", "claude-haiku-4.5", Lane.THIN, "claude", 4,
         Cost(0.0, 91, 0.74),
         "Fastest rung measured. Default first attempt in the thin lane."),

    # --- thin lane · kirocc · GPT family ----------------------------------
    # These exist for one structural reason: a verifier must not share a
    # family with the generator. See gates/ and docs/principles.md.
    Tier("gpt-5.6-sol", "kirocc", "gpt-5.6-sol", Lane.THIN, "gpt", 2,
         Cost(0.0, 91, 0.75),
         "Cross-family verifier for Claude-generated work."),
    Tier("gpt-5.6-terra", "kirocc", "gpt-5.6-terra", Lane.THIN, "gpt", 2,
         Cost(0.0, 91, 0.78)),
    Tier("gpt-5.6-luna", "kirocc", "gpt-5.6-luna", Lane.THIN, "gpt", 3,
         Cost(0.0, 91, 0.78)),

    # --- thick lane · claude -p -------------------------------------------
    # Every rung here pays the same 22.8k harness tax. The dollar figures are
    # the measured floor for a one-word answer, so real work costs more — but
    # the floor is what makes routing a classification job here indefensible.
    Tier("cli-opus-4.5", "claude-cli", "claude-opus-4-5", Lane.THICK, "claude", 0,
         Cost(0.2297, 22_804, 8.0),
         "Measured floor $0.2297 for one word. Reserve for work that earns it."),
    Tier("cli-sonnet-4.5", "claude-cli", "claude-sonnet-4-5", Lane.THICK, "claude", 2,
         Cost(0.1000, 22_800, 6.0),
         "Interpolated usd — not measured. Probe before trusting it."),
    Tier("cli-haiku-4.5", "claude-cli", "claude-haiku-4-5-20251001", Lane.THICK, "claude", 4,
         Cost(0.0468, 22_810, 5.0),
         "Measured floor $0.0468 for one word: 100% harness, 0% answer."),
)

BY_NAME = {tier.name: tier for tier in LADDER}


def lane(name: str) -> str:
    return BY_NAME[name].lane


def in_lane(lane_name: str) -> list[Tier]:
    return [t for t in LADDER if t.lane == lane_name]


def climb_order(lane_name: str, *, family: str | None = None,
                weights: dict | None = None) -> list[Tier]:
    """Rungs cheapest-first, which is the order a cascade tries them.

    Ties broken by `rank` so that two rungs of equal measured cost are still
    ordered weakest-first — climbing should be monotone in capability, or an
    escalation can land on a rung no better than the one that just failed.
    """
    weights = weights or {}
    rungs = [t for t in in_lane(lane_name)
             if family is None or t.family == family]
    return sorted(rungs, key=lambda t: (t.cost.objective(**weights), -t.rank))


def escalation_path(start: str, *, weights: dict | None = None) -> list[Tier]:
    """The rungs above `start`, in the order to try them after a gate failure.

    Strictly stronger only. A failed rung must never escalate sideways into a
    rung of equal rank: the second attempt would be a re-roll dressed up as a
    correction, and the run record would show two failures where there was one
    coin flipped twice.
    """
    here = BY_NAME[start]
    stronger = [t for t in in_lane(here.lane) if t.rank < here.rank]
    weights = weights or {}
    return sorted(stronger, key=lambda t: (t.cost.objective(**weights), -t.rank))


def counter_family(tier_name: str) -> list[Tier]:
    """Rungs a verifier may use against work generated on `tier_name`.

    Different family, same lane. This is the mechanical half of the gate rule:
    an evaluator's harmful self-preference concentrates exactly where it errs
    as a generator (arXiv:2504.03846), and self-recognition drives self-
    preference (arXiv:2404.13076). Same-family verification is therefore
    weakest precisely where it is needed, so the gate refuses it.
    """
    here = BY_NAME[tier_name]
    return sorted((t for t in in_lane(here.lane) if t.family != here.family),
                  key=lambda t: t.rank)
