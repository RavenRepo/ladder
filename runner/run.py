"""The loop: select, dispatch, gate, escalate, record, stop.

The stop condition is written before the prompt, in counts rather than
adjectives, because a setup that knows how to start and not how to finish is a
runaway process rather than a loop.

Everything that spends a dispatch lands in the run record, including malformed
replies which are neither retried nor escalated. A run record showing a clean
stop while dispatches went unaccounted for is worse than no record at all, and
`40-runs/` is the only thing the weekly meta-review reads.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import random
import traceback

from . import dispatch as dispatch_mod
from . import policy as policy_mod
from .gate import CHECKABLE, JUDGMENT, ModelVerifier, NullVerifier, escalate, run_gate
from .policy import Outcome
from .substrate import available as live_surfaces
from .tiers import BY_NAME, escalation_path

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = ROOT / "40-runs"
RETURNS = ROOT / "10-returns"


@dataclasses.dataclass
class Stop:
    """The ending, written first."""

    max_dispatches: int = 60
    max_seconds: float = 1800.0
    max_attempts: int = 2          # per instance, per the article's retry cap
    verified_target: int | None = None


@dataclasses.dataclass
class RunResult:
    run_id: str
    dispatched: int = 0
    passed: int = 0
    quality_failed: int = 0
    transport_failed: int = 0
    malformed: int = 0
    escalated: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    seconds: float = 0.0
    stopped_because: str = ""
    unfinished: list[str] = dataclasses.field(default_factory=list)
    explored: int = 0


def _run_id(name: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{name}"


def execute(*, name: str, capability: str, lane: str, floor_tier: str,
            instances: list[str], prompt_for, skill: str, constraints: str,
            schema_block: str, stop: Stop | None = None,
            dispatcher_name: str | None = None, gate_level: str = "script",
            weights: dict | None = None, seed: int | None = None,
            dispatcher_kwargs: dict | None = None,
            task_description: str = "") -> RunResult:
    """Run one capability across a set of instances.

    `gate_level` is `script` (free, gate 1 only), `checkable` (gates 1-2), or
    `full` (gates 1-3). Default is `script` so that a first run of anything
    costs one dispatch per instance and no verifier calls — find the broken
    launch before paying to verify its output.
    """
    stop = stop or Stop()
    weights = weights or {}
    rng = random.Random(seed)
    started = dt.datetime.now(dt.timezone.utc)
    run_id = _run_id(name)
    result = RunResult(run_id=run_id)

    surfaces = set(live_surfaces()) or None
    if dispatcher_name == "mock":
        surfaces = None                     # the mock has no surface to be live

    checkable = NullVerifier(CHECKABLE)
    judgment = NullVerifier(JUDGMENT)
    if gate_level in ("checkable", "full"):
        checkable = ModelVerifier(CHECKABLE, available=surfaces)
    if gate_level == "full":
        judgment = ModelVerifier(JUDGMENT, available=surfaces)

    outcomes = policy_mod.load()
    verdicts_for_queue = []
    pending = [(instance, 1, None, None) for instance in instances]

    try:
        while pending:
            if result.dispatched >= stop.max_dispatches:
                result.stopped_because = f"dispatch cap {stop.max_dispatches} reached"
                result.unfinished = [i for i, *_ in pending]
                break
            elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
            if elapsed >= stop.max_seconds:
                result.stopped_because = f"wall clock cap {stop.max_seconds:.0f}s reached"
                result.unfinished = [i for i, *_ in pending]
                break

            batch, pending = pending[:stop.max_dispatches], pending[stop.max_dispatches:]
            works = []
            for instance, attempt, forced_tier, reason in batch:
                if forced_tier:
                    tier, exploring = forced_tier, False
                else:
                    decision = policy_mod.choose(
                        capability, lane=lane, floor_tier=floor_tier,
                        outcomes=outcomes, weights=weights, rng=rng,
                        available=surfaces)
                    tier, exploring = decision.tier, decision.exploring
                work = dispatch_mod.Work(
                    capability=capability, instance=instance, tier=tier,
                    prompt=prompt_for(instance), attempt=attempt,
                    failure_reason=reason, exploring=exploring)
                works.append(work)

            # One dispatcher per surface in the batch: a batch may mix rungs
            # when exploration fires, and kirocc and claude-cli are not
            # interchangeable transports.
            by_surface: dict[str, list] = {}
            for work in works:
                surface = dispatcher_name or BY_NAME[work.tier].surface
                prompt = dispatch_mod.build_prompt(
                    work, skill=skill, constraints=constraints,
                    schema_block=schema_block, lane=lane)
                by_surface.setdefault(surface, []).append((work, prompt))

            returns = []
            for surface, items in by_surface.items():
                dispatcher = dispatch_mod.get(surface, **(dispatcher_kwargs or {}))
                returns.extend(dispatcher.run(items))

            _save_returns(RETURNS / run_id, returns)

            for ret in returns:
                result.dispatched += 1
                result.cost_usd += ret.cost_usd
                result.tokens_in += ret.tokens_in
                if ret.work.exploring:
                    result.explored += 1

                verdict = run_gate(ret, checkable=checkable, judgment=judgment,
                                   constraints=constraints, task=task_description,
                                   source_text=prompt_for(ret.work.instance))
                outcome = Outcome(
                    capability=capability, tier=ret.work.tier,
                    verdict=("pass" if verdict.ok else verdict.failure_class),
                    gate_stage=verdict.stage, reason=verdict.reason_text,
                    cost_usd=ret.cost_usd, seconds=ret.seconds,
                    run_id=run_id, instance=ret.work.instance)
                policy_mod.record(outcome)
                outcomes.append(outcome)

                if verdict.ok:
                    result.passed += 1
                    continue

                klass = verdict.failure_class
                if klass == "transport":
                    result.transport_failed += 1
                    if ret.work.attempt < stop.max_attempts:
                        # Retried CLEAN. There is nothing to correct, and
                        # telling an agent its last attempt timed out is noise
                        # that can only make the next answer worse.
                        pending.append((ret.work.instance, ret.work.attempt + 1,
                                        None, None))
                    else:
                        result.unfinished.append(ret.work.instance)
                    continue

                if klass == "malformed":
                    # Not retried, not escalated — but recorded, because it
                    # spent a dispatch and the meta-review must be able to see
                    # that it did.
                    result.malformed += 1
                    continue

                result.quality_failed += 1
                if ret.work.attempt < stop.max_attempts:
                    # Retried WITH the reason, one rung up. Passing the reason
                    # back is what turns a retry into a correction; climbing is
                    # what stops it being the same coin flipped twice.
                    upward = [t for t in escalation_path(ret.work.tier, weights=weights)
                              if surfaces is None or t.surface in surfaces]
                    next_tier = upward[0].name if upward else ret.work.tier
                    pending.append((ret.work.instance, ret.work.attempt + 1,
                                    next_tier, verdict.reason_text))
                else:
                    verdicts_for_queue.append(verdict)

            if stop.verified_target and result.passed >= stop.verified_target:
                result.stopped_because = f"reached {stop.verified_target} verified"
                result.unfinished = [i for i, *_ in pending]
                break

        if not result.stopped_because:
            result.stopped_because = "work list exhausted"

    except Exception:                                        # noqa: BLE001
        # A crashed run still spent money and may already have recorded
        # outcomes. Write the record, then re-raise.
        result.stopped_because = f"crashed: {traceback.format_exc(limit=3)}"
        result.escalated = escalate(verdicts_for_queue, run_id)
        result.seconds = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        write_record(result, capability=capability, lane=lane,
                     floor_tier=floor_tier, gate_level=gate_level)
        raise

    result.escalated = escalate(verdicts_for_queue, run_id)
    result.seconds = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    write_record(result, capability=capability, lane=lane,
                 floor_tier=floor_tier, gate_level=gate_level)
    return result


def _save_returns(directory: pathlib.Path, returns: list) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for ret in returns:
        slug = "".join(c if c.isalnum() or c in "-_" else "-"
                       for c in ret.work.instance)[:60]
        path = directory / f"{slug}-a{ret.work.attempt}.json"
        path.write_text(json.dumps({
            "instance": ret.work.instance,
            "tier": ret.work.tier,
            "attempt": ret.work.attempt,
            "ok": ret.ok,
            "error": ret.error,
            "seconds": round(ret.seconds, 2),
            "cost_usd": ret.cost_usd,
            "tokens_in": ret.tokens_in,
            "record": ret.record,
            "raw": ret.raw[:4000],
        }, indent=2) + "\n")


def write_record(result: RunResult, *, capability: str, lane: str,
                 floor_tier: str, gate_level: str) -> pathlib.Path:
    """`40-runs/<run-id>.md` — the audit trail, and the meta-review's only input."""
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"{result.run_id}.md"
    lines = [
        f"# {result.run_id}",
        "",
        f"- capability: `{capability}`",
        f"- lane: `{lane}` · floor tier: `{floor_tier}` · gate: `{gate_level}`",
        f"- stopped because: {result.stopped_because}",
        f"- wall clock: {result.seconds:.1f}s",
        "",
        "## Dispatches",
        "",
        "| outcome | count |",
        "|---|---|",
        f"| passed | {result.passed} |",
        f"| quality failed | {result.quality_failed} |",
        f"| transport failed | {result.transport_failed} |",
        f"| malformed | {result.malformed} |",
        f"| **total dispatched** | **{result.dispatched}** |",
        f"| of which exploring one rung cheaper | {result.explored} |",
        "",
        "## Cost",
        "",
        f"- metered: ${result.cost_usd:.4f}",
        f"- input tokens: {result.tokens_in:,}",
        "",
        f"## Escalated to needs-human.md: {result.escalated}",
        "",
    ]
    if result.unfinished:
        lines += ["## Unfinished", ""]
        lines += [f"- {item}" for item in result.unfinished]
        lines.append("")
    path.write_text("\n".join(lines))
    return path
