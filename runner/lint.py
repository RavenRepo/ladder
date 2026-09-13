"""The cheapest check in the workspace. Run it after editing any launch.

It exists because of one specific defect, found by a live run rather than by
reading: **a launch can ask for evidence in a form its own gate rejects.**

`00-launches/triage-failures.md` asked agents to cite "the exact substring that
decides it". Gate 1 accepted only locators — urls and `path#Lnn`. Every
correct answer was rejected as unlocatable. Sixteen dispatches, ten quality
failures, two escalations to the human queue, and not one of them was the
agents' fault. The gate was wrong and the launch was right, and nothing in the
system noticed because each file was internally consistent.

So the linter does not check launches against a style guide. It checks them
against the **gate that will actually judge their output**, by constructing a
representative return for the launch and running gate 1 on it. If the launch's
own example return cannot pass its own gate, nothing it dispatches will.
"""

from __future__ import annotations

import json
import re

from .gate import check_shape
from .launch import Launch, list_launches
from . import substrate
from .tiers import BY_NAME, counter_family, escalation_path

_JSONISH = re.compile(r"\{.*\}", re.DOTALL)


def lint(launch: Launch) -> list[str]:
    """Findings for one launch. Empty means runnable, not means correct."""
    findings = list(launch.validate())

    # 1. Does the launch's own declared return schema pass its own gate?
    findings += _check_example_return(launch)

    # 2. Is there a rung to escalate to when the first attempt fails?
    if launch.floor_tier in BY_NAME:
        upward = escalation_path(launch.floor_tier, weights=launch.weights)
        if not upward and launch.max_attempts > 1:
            findings.append(
                f"floor_tier {launch.floor_tier!r} is the top of the "
                f"{launch.lane} ladder, but max_attempts is {launch.max_attempts}. "
                "A quality retry has nowhere to climb and will re-run the same "
                "rung — a re-roll, not a correction. Set max_attempts: 1 or "
                "lower the floor.")

        # 3. If the gate will call a verifier, does a cross-family rung exist?
        #
        # The two stages want different things and must be checked separately.
        # While the thick lane held Claude rungs only, "is counter_family empty"
        # happened to answer both questions at once. It stopped doing so the
        # moment a mid-rank rung of another family was added: gate 2 is now
        # satisfied there and gate 3 still is not, and a launch that passed this
        # check would send every single return to a human while reporting clean.
        # A launch is not checked at its floor alone. A quality retry escalates,
        # so the rung generating the work at gate time is frequently a rung the
        # floor merely leads to — and gate 3's judge has to be peer-or-stronger
        # than THAT. Checking the floor only lints clean and then refuses on the
        # first retry. Check #2 above already walks the same path.
        generators = [BY_NAME[launch.floor_tier]]
        if launch.max_attempts > 1:
            generators += escalation_path(launch.floor_tier, weights=launch.weights)

        # And a rung whose surface the probe found dead is not a verifier.
        # `ModelVerifier.pick` filters by live surfaces; a check that does not
        # is answering a different question from the one the run will ask.
        # Without this the thick lane's whole gate hangs on one optional
        # third-party binary and the cheapest check in the workspace is silent
        # about it.
        live = set(substrate.available())
        for here in generators:
            others = [t for t in counter_family(here.name)
                      if not live or t.surface in live]
            where = ("" if here.name == launch.floor_tier
                     else f" (reached by escalation from {launch.floor_tier!r})")
            if launch.gate in ("checkable", "full") and not others:
                dead = [t.surface for t in counter_family(here.name)]
                why = (f"the {launch.lane} lane has no rung outside "
                       f"{here.family!r}" if not dead else
                       f"every rung outside {here.family!r} is on a surface the "
                       f"probe did not find live ({', '.join(sorted(set(dead)))})")
                findings.append(
                    f"gate {launch.gate!r} needs a verifier of a different model "
                    f"family for {here.name!r}{where}, and {why}. Every return "
                    "would fail closed at gate 2.")
                continue
            if launch.gate == "full" and others:
                # Gate 3 refuses rather than falling back to a weaker judge, so
                # a counter-family rung that is merely present is not enough: it
                # has to be peer-or-stronger than the rung that generated it.
                if not [t for t in others if t.rank <= here.rank]:
                    strongest = min(others, key=lambda t: t.rank)
                    findings.append(
                        f"gate 'full' needs a judge of a different model family "
                        f"at {here.name!r}{where}'s rank or better. The "
                        f"{launch.lane} lane's strongest live rung outside "
                        f"{here.family!r} is {strongest.name!r} (rank "
                        f"{strongest.rank} vs {here.rank}), so gate 3 refuses "
                        "and every return goes to the human queue. Lower the "
                        "floor or use gate 'checkable'.")

    # 4. A stop condition made of counts, not adjectives.
    if launch.max_dispatches <= 0:
        findings.append("max_dispatches must be positive or nothing is dispatched")
    if launch.max_dispatches < len(launch.instances):
        findings.append(
            f"max_dispatches ({launch.max_dispatches}) is below the instance "
            f"count ({len(launch.instances)}), so this launch cannot finish in "
            "one run even with no retries. Intentional?")

    # 5. Retries need headroom or the cap is the real limit.
    needed = len(launch.instances) * launch.max_attempts
    if launch.max_dispatches < needed:
        findings.append(
            f"note: {len(launch.instances)} instances x {launch.max_attempts} "
            f"attempts = {needed} possible dispatches, above the cap of "
            f"{launch.max_dispatches}. Retries may be cut off by the cap rather "
            "than by the retry policy.")

    return findings


def _check_example_return(launch: Launch) -> list[str]:
    """Run gate 1 on the launch's own example return.

    This is the check that would have saved the first live run. The example in
    the `## return` section is what every agent copies, so if it cannot pass
    gate 1, neither can they.
    """
    block = launch.return_schema.strip()
    if not block:
        return ["no `## return` section: agents have no schema to return"]

    match = _JSONISH.search(block)
    if not match:
        return ["the `## return` section contains no JSON object"]

    # The example is written for humans and often carries placeholders — `0.0`,
    # `<the reason>`, `or null`. Parse what we can; a schema that is not even
    # approximately JSON is itself the finding.
    text = match.group(0)
    text = re.sub(r'"\s*or\s+null', '"', text)
    text = re.sub(r"<[^>]*>", "placeholder", text)
    try:
        example = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"the `## return` example is not parseable JSON ({exc.msg}). "
                "Agents copy this block verbatim; if it will not parse, their "
                "returns will not either."]

    findings = []
    # The launch's own prompt is the source text an agent would be quoting
    # from, with one instance interpolated. That is what the gate will use.
    source = launch.prompt_for(launch.instances[0]) if launch.instances else ""
    errors = check_shape(example, source_text=source)
    for error in errors:
        if "does not appear in the source text" in error:
            # Expected: the example's evidence is a placeholder, not a real
            # quotation. Not a finding.
            continue
        findings.append(
            f"the launch's own example return fails gate 1: {error}. "
            "Nothing this launch dispatches can pass.")

    # The specific defect this file exists for.
    wants_quote = re.search(r"\b(cite|quote|verbatim|exact substring|excerpt)\b",
                            launch.prompt_template, re.IGNORECASE)
    if wants_quote and not launch.instances:
        findings.append(
            "the prompt asks for a quotation but the launch has no instances, "
            "so gate 1 will have no source text to check quotations against and "
            "will reject every one of them as unlocatable.")
    return findings


def lint_all() -> dict[str, list[str]]:
    return {launch.name: lint(launch) for launch in list_launches()}
