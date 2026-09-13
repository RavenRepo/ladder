"""The gate. Outside the generating agent, by construction, at four costs.

An agent rereading its own output sees every reason it wrote things that way,
so it approves. That is not a quirk of one model — an evaluator's harmful
self-preference concentrates precisely where it errs as a generator, and
stronger models show it *more* on their own mistakes (arXiv:2504.03846). Self-
judgment therefore fails in exactly the case a gate exists to catch. So nothing
here ever runs in the context that produced the return, and stages 2 and 3
additionally refuse to run on the generator's own model family.

Order is by cost, cheapest first, because the script is free and should reject
everything it can before a token is spent.

    1 script      deterministic code           free
    2 checkable   cheap rung, other family     one call
    3 judgment    peer-or-stronger, other fam  one call, only if 1-2 passed
    4 human       needs-human.md               your five minutes a day

Stage 2 and stage 3 are separated on purpose. Verification is easier than
generation for mechanically confirmable facts — does the cited file exist, does
the evidence line support the statement — and it is *not* easier for judgment.
So a cheap rung is admissible for the first and inadmissible for the second.
The governing formalisation is the generation-verification gap
(arXiv:2412.02674), which also reports that self-improvement by self-filtering
saturates: this loop will not keep paying forever.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re

from .dispatch import Return, extract_json, for_tier, Work
from .tiers import BY_NAME, counter_family

ROOT = pathlib.Path(__file__).resolve().parent.parent
NEEDS_HUMAN = ROOT / "30-queries" / "needs-human.md"

SCRIPT, CHECKABLE, JUDGMENT, PASSED = "script", "checkable", "judgment", "pass"
DEFERRED = "deferred"

# The ONE use of a model's self-reported confidence that the evidence licenses.
#
# Verbalized confidence is badly calibrated — around 0.10 ECE at best for 70B+
# models, worse below that, and dominated by how the question was phrased
# (arXiv:2412.14737) — and the bias runs toward OVERCONFIDENCE
# (arXiv:2604.01457). So it is usable only asymmetrically:
#
#   low confidence  -> escalate.  A model saying "I am unsure" is cheap, rare,
#                      and the one direction its bias does not manufacture.
#   high confidence -> NOTHING.   Never a pass, never a tiebreak, never a
#                      threshold. Overconfidence is the failure mode, so the
#                      confident direction carries no information.
LOW_CONFIDENCE = 0.5


@dataclasses.dataclass
class Verdict:
    ret: Return
    stage: str
    ok: bool
    reasons: list[str]
    retryable: bool
    verifier_tier: str | None = None

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)

    @property
    def failure_class(self) -> str:
        """Which of the three destinations this belongs in.

        Lumping these together produces a human queue full of things no human
        can act on, and a queue that is not actionable does not get read.
        """
        if self.ok:
            return "pass"
        if self.stage == DEFERRED:
            # The model flagged its own uncertainty and we took it seriously.
            # This is NOT a quality failure: counting it against the rung would
            # punish an honest signal and train the next model to overclaim,
            # which is the one thing this gate cannot detect.
            return "deferred"
        if self.ret.transport_error:
            return "transport"
        if self.ret.record is None:
            return "malformed"
        return "quality"


# --- gate 1 · deterministic script ----------------------------------------
# Never makes a truth claim. Only checks shape, and checks it for free.

_LOCATOR = re.compile(r"^\S+://\S+|^[\w./-]+#L\d+|^[\w./-]+:\d+")

# Evidence comes in exactly two legitimate kinds and the gate accepts both.
#
#   LOCATOR    a url, `path#Lnn`, or `path:nn` — something a third party can go
#              and open. Checked by shape here; checked for existence at gate 2.
#   QUOTATION  a verbatim substring of the material the task supplied. Checked
#              here, for free, against the source text: a quotation that does
#              not occur in the source is a fabricated citation and is caught
#              without spending a single verifier token.
#
# The first live run is what forced this. The launch asked agents to cite "the
# exact substring that decides it" while the gate accepted only locators, so
# every correct answer was rejected — a launch demanding evidence its own gate
# refuses. The models were right and the gate was wrong. `ladder lint` now
# catches that contradiction before a run spends anything.

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Whitespace-insensitive, case-insensitive comparison.

    A model that re-wraps a quotation across lines has not fabricated it, and
    rejecting that would push agents toward shorter, less useful citations.
    """
    return _WS.sub(" ", text).strip().casefold()


def check_shape(record: dict, source_text: str | None = None) -> list[str]:
    """Gate 1. Free, deterministic, and never a truth claim.

    `source_text` is the material the task supplied — the instance text, and
    anything quoted into the prompt. When it is given, quotations are checked
    against it. When it is not, quotations cannot be verified and only locators
    are accepted, because unverifiable evidence is how a citation becomes a
    decoration.
    """
    errors: list[str] = []
    for field in ("capability", "instance", "claims"):
        if field not in record:
            errors.append(f"missing field: {field}")
    if errors:
        return errors

    claims = record["claims"]
    if not isinstance(claims, list) or not claims:
        return ["claims must be a non-empty list"]

    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            errors.append(f"claims[{index}] must be an object")
            continue
        if not str(claim.get("statement", "")).strip():
            errors.append(f"claims[{index}] has no statement")
        evidence = str(claim.get("evidence", "")).strip()
        if not evidence:
            # A claim with no source line is not a claim. This is the check
            # that makes every later stage possible: stage 2 verifies evidence,
            # and it cannot verify what was never cited.
            errors.append(f"claims[{index}] has no evidence line")
        elif not _LOCATOR.match(evidence):
            if source_text is None:
                errors.append(
                    f"claims[{index}] evidence is not a locatable reference "
                    f"(want url, path#Lnn or path:nn), and no source text was "
                    f"supplied to check it as a quotation: {evidence[:60]!r}"
                )
            elif _normalise(evidence) not in _normalise(source_text):
                # The expensive-sounding check that costs nothing: a quotation
                # that is not in the source is a fabricated citation, and it is
                # caught here rather than by a verifier that has to be paid.
                errors.append(
                    f"claims[{index}] evidence is quoted but does not appear in "
                    f"the source text: {evidence[:60]!r}"
                )

    errors += check_no_fake_completion(record)

    confidence = record.get("confidence")
    if confidence is not None:
        # Carried for the human queue to read. Nothing automated may branch on
        # it — see SCHEMA.md. It is still range-checked, because a model
        # emitting 1.7 is telling you something about its grip on the schema.
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            errors.append(f"confidence must be between 0 and 1, got {confidence!r}")
    return errors


# --- gate 1b · the cheat markers, checked for free -------------------------
#
# Frontier models cheat on multi-file repository tasks at measured rates around
# 49-54% (arXiv:2510.20270), and the useful part of that finding is that most
# of the exploits are SYNTACTICALLY VISIBLE: a stub that returns the literal a
# test expects, a `test.skip` added beside the code it was meant to cover, a
# TODO standing in for the branch that was the actual task.
#
# Visible means a regex finds them, which means they never need to reach a
# model at all. Everything below runs at gate 1, for nothing, before a verifier
# token is spent — and it is the same rule as "no fake completion": a
# placeholder is a blocker, not evidence.

_CHEAT_MARKERS = (
    (re.compile(r"\btest\s*\.\s*(skip|only)\b", re.I),
     "a skipped or exclusive test"),
    (re.compile(r"\b(TODO|FIXME|XXX|HACK)\b"),
     "a placeholder marker"),
    (re.compile(r"\bNotImplementedError\b"),
     "an unimplemented branch"),
    (re.compile(r"\braise\s+NotImplemented\b"),
     "an unimplemented branch"),
    (re.compile(r"\b(stub|placeholder|dummy)\s+(implementation|function|value)\b", re.I),
     "a self-described stub"),
    (re.compile(r"\b(for\s+now|left\s+as\s+an\s+exercise|will\s+implement\s+later)\b", re.I),
     "deferred work described as done"),
)


def check_no_fake_completion(record: dict, artifact_text: str | None = None) -> list[str]:
    """Reject work that reports completion while containing its own placeholder.

    Scans the record's own claims, and any artifact text the caller supplies
    (a diff, a file body). This is deliberately a blunt instrument: a claim
    whose text says TODO is either describing a placeholder it left behind, or
    describing one it found — and the second case belongs in the statement,
    not the evidence, so the cost of the false positive is one clarifying
    rewrite.
    """
    errors: list[str] = []
    haystacks = []
    for index, claim in enumerate(record.get("claims") or []):
        if isinstance(claim, dict):
            haystacks.append((f"claims[{index}]", str(claim.get("statement", ""))))
    if artifact_text:
        haystacks.append(("artifact", artifact_text))

    for where, text in haystacks:
        for pattern, what in _CHEAT_MARKERS:
            match = pattern.search(text)
            if match:
                errors.append(
                    f"{where} reports completion but contains {what} "
                    f"({match.group(0)!r}). A placeholder is a blocker, not "
                    f"evidence — implement it or report it as unfinished.")
                break            # one finding per haystack is enough to reject
    return errors


# --- gates 2 and 3 · model verifiers --------------------------------------

CHECKABLE_PROMPT = """You are gate 2 of 4. You check MECHANICALLY CONFIRMABLE facts only.

You did not write this record and you must not improve it. Your only question:
for each claim, does the cited evidence exist and does it actually support the
statement as written?

DO NOT judge whether the approach is good, whether the reasoning is sound, or
whether you would have done it differently. That is gate 3's job and you will
be wrong about it.

Reject a claim when:
  - the evidence line points at something that does not exist
  - the evidence exists but does not support the statement
  - the statement asserts more than the evidence shows

Return exactly this JSON and nothing else:
{"ok": true|false, "reasons": ["one line per rejected claim"]}

THE RECORD:
"""

JUDGMENT_PROMPT = """You are gate 3 of 4. Gates 1 and 2 already passed: the shape is
valid and every claim's evidence exists and supports it.

Your question is the one a cheap check cannot answer: is this RIGHT? Is the
reasoning sound, is the evidence thin for what is being concluded, was the
wrong thing done competently?

You did not write this record. Do not rewrite it. Judge it.

Reject when:
  - the conclusion does not follow from the claims
  - the evidence is technically present but too thin to carry the weight
  - the record answers a different question than the one asked
  - a constraint in the CONSTRAINTS block was violated

Return exactly this JSON and nothing else:
{"ok": true|false, "reasons": ["one line per problem"]}

CONSTRAINTS IN FORCE:
{constraints}

THE TASK THAT WAS ASKED:
{task}

THE RECORD:
"""


class ModelVerifier:
    """A verifier rung. Refuses to share a family with the generator.

    `counter_family()` is what enforces it, and the refusal is hard rather than
    a preference: same-family verification is weakest exactly where it is most
    needed (arXiv:2504.03846, arXiv:2404.13076).
    """

    def __init__(self, stage: str, *, timeout: int = 180,
                 available: set[str] | None = None):
        self.stage = stage
        self.timeout = timeout
        self.available = available

    def pick(self, generator_tier: str) -> str | None:
        """Choose the verifier rung for work generated on `generator_tier`.

        Stage 2 takes the cheapest rung of the other family — checkable claims
        do not need strength. Stage 3 takes a peer-or-stronger one, because
        judgment does.
        """
        candidates = counter_family(generator_tier)
        if self.available is not None:
            candidates = [t for t in candidates if t.surface in self.available]
        if not candidates:
            return None
        if self.stage == CHECKABLE:
            return max(candidates, key=lambda t: t.rank).name

        here = BY_NAME[generator_tier]
        strong = [t for t in candidates if t.rank <= here.rank]
        # No peer-or-stronger rung of the other family: gate 3 REFUSES rather
        # than falling back to a weaker one.
        #
        # An earlier version returned the strongest available candidate here,
        # which put a weak verifier on a strong generator's judgment — the
        # worst configuration there is. Verification skill tracks the verifier's
        # own generation ability, and errors produced by a *stronger* generator
        # are the hardest to detect, because they are internally consistent and
        # wrong (arXiv:2509.17995). A model is also a worse verifier than solver
        # of the same problem (arXiv:2502.14948). So a weaker judge does not
        # give you a weaker gate; it gives you a gate that passes precisely the
        # errors it was installed to catch, while reporting success.
        #
        # Returning None routes the record to the human queue instead. That is
        # the correct answer: when the strongest rung you have produces work,
        # nothing you own can judge it, and a person has to.
        return strong[0].name if strong else None

    def __call__(self, ret: Return, *, constraints: str = "",
                 task: str = "") -> tuple[bool, list[str], bool, str | None]:
        tier = self.pick(ret.work.tier)
        if tier is None:
            # No admissible verifier. Refusing is the safe direction: passing a
            # record because no verifier was available would mark it verified on
            # the strength of an outage. `retryable=False` because retrying
            # changes nothing — the ladder has no rung that may judge this, and
            # only a person can.
            if self.stage == JUDGMENT:
                return False, [
                    f"no peer-or-stronger verifier outside the "
                    f"{BY_NAME[ret.work.tier].family!r} family exists for "
                    f"{ret.work.tier!r}. A weaker judge would pass exactly the "
                    f"errors this gate exists to catch (arXiv:2509.17995), so "
                    f"this needs a human."
                ], False, None
            return False, ["no cross-family verifier rung is available"], True, None

        if self.stage == CHECKABLE:
            prompt = CHECKABLE_PROMPT + json.dumps(ret.record, indent=2)
        else:
            prompt = (JUDGMENT_PROMPT
                      .replace("{constraints}", constraints or "(none)")
                      .replace("{task}", task or "(not supplied)")
                      + json.dumps(ret.record, indent=2))

        probe = Work(capability=f"gate:{self.stage}", instance=ret.work.instance,
                     prompt="", tier=tier)
        dispatcher = for_tier(tier, timeout=self.timeout, concurrency=1)
        results = dispatcher.run([(probe, prompt)])
        if not results:
            return False, ["verifier returned nothing"], True, tier

        reply = results[0]
        if reply.transport_error:
            # The verifier never answered. That says nothing about the record,
            # so it must not be recorded as a quality failure.
            return False, [f"verifier transport failure: {reply.error}"], True, tier

        verdict = reply.record or extract_json(reply.raw)
        if not isinstance(verdict, dict) or "ok" not in verdict:
            return False, ["verifier did not return the verdict schema"], True, tier
        if verdict.get("ok"):
            return True, [], False, tier
        reasons = verdict.get("reasons") or ["verifier rejected without a reason"]
        return False, [str(r) for r in reasons][:6], True, tier


class NullVerifier:
    """Gates 2 and 3 turned off. For mock runs and for `--gate script`."""

    def __init__(self, stage: str, **_):
        self.stage = stage

    def pick(self, generator_tier: str) -> str | None:
        return None

    def __call__(self, ret: Return, **_) -> tuple[bool, list[str], bool, str | None]:
        return True, [], False, None


# --- the ladder of gates --------------------------------------------------

def run_gate(ret: Return, *, checkable=None, judgment=None,
             constraints: str = "", task: str = "",
             source_text: str | None = None) -> Verdict:
    """Run the stages in cost order and stop at the first rejection.

    `source_text` is whatever material the task supplied, so gate 1 can check
    quoted evidence against it for free.
    """
    if ret.transport_error:
        return Verdict(ret, SCRIPT, False, [ret.error or "transport failure"], True)
    if ret.record is None:
        # It answered, but not in the schema. Not retried: there is no
        # correction to hand back, and the return was never a record.
        return Verdict(ret, SCRIPT, False, [ret.error or "no record in the reply"], False)

    errors = check_shape(ret.record, source_text=source_text)
    if errors:
        return Verdict(ret, SCRIPT, False, errors, True)

    checkable = checkable or NullVerifier(CHECKABLE)
    ok, reasons, retryable, tier = checkable(ret, constraints=constraints, task=task)
    if not ok:
        return Verdict(ret, CHECKABLE, False, reasons, retryable, tier)

    judgment = judgment or NullVerifier(JUDGMENT)
    ok, reasons, retryable, tier = judgment(ret, constraints=constraints, task=task)
    if not ok:
        return Verdict(ret, JUDGMENT, False, reasons, retryable, tier)

    # Last, and only in the one admissible direction: a model that says it is
    # unsure gets escalated rather than believed. Checked AFTER the real gates
    # so that a confident wrong answer is still caught on its merits — the
    # confidence field never substitutes for a check.
    stated = ret.record.get("confidence")
    if isinstance(stated, (int, float)) and stated < LOW_CONFIDENCE:
        return Verdict(ret, DEFERRED, False,
                       [f"the agent reported confidence {stated:.2f}, below "
                        f"{LOW_CONFIDENCE}. Escalating rather than accepting a "
                        f"self-declared guess."],
                       True, tier)

    return Verdict(ret, PASSED, True, [], False, tier)


def escalate(verdicts: list[Verdict], run_id: str,
             path: pathlib.Path = NEEDS_HUMAN) -> int:
    """Append twice-failed work to the human queue.

    Appends under whatever header is already there and writes a header only
    into an empty file. This file is the one a human writes in — resolutions,
    notes, decisions — and nothing here may rewrite it.
    """
    # `deferred` reaches here only when the ladder ran out of rungs, which is
    # exactly the case a person has to decide.
    actionable = [v for v in verdicts
                  if v.failure_class in ("quality", "deferred")]
    if not actionable:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = []
    if not existing.strip():
        lines.append("# needs-human\n\nWork that failed the gate twice. "
                     "Decide it, write the decision under the entry, and move on.\n")
    for verdict in actionable:
        work = verdict.ret.work
        lines.append(
            f"\n## {work.capability} · {work.instance}\n"
            f"- run: `{run_id}`\n"
            f"- tier: `{work.tier}`"
            + (f" · verifier: `{verdict.verifier_tier}`" if verdict.verifier_tier else "")
            + f"\n- failed at gate: **{verdict.stage}** after {work.attempt} attempts\n"
            f"- reason: {verdict.reason_text}\n\n"
            f"**Decision:** _(yours)_\n"
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(lines))
    return len(actionable)
