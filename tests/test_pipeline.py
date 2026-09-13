"""Tests. Every one of these is a defect that was possible before it was a rule.

Run with: python3 -m unittest discover -s tests -t . -v

They spend nothing: the mock dispatcher's `tier_skill` makes a cheap rung
genuinely worse than an expensive one, which is the only way to exercise the
promote/demote logic without a month of real runs.
"""

from __future__ import annotations

import pathlib
import random
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from runner import dispatch as dispatch_mod           # noqa: E402
from runner import policy as policy_mod               # noqa: E402
from runner import tiers                              # noqa: E402
from runner.gate import (CHECKABLE, JUDGMENT, ModelVerifier,  # noqa: E402
                         check_shape, run_gate)
from runner.dispatch import Return, Work              # noqa: E402
from runner.policy import Outcome, choose, tally      # noqa: E402


class TestLadderInvariants(unittest.TestCase):

    def test_escalation_is_monotone_in_capability(self):
        """A failed rung may never escalate sideways to an equal rank.

        Otherwise the retry is a re-roll wearing a correction's clothes, and
        the run record shows two failures where there was one coin flipped
        twice.
        """
        for tier in tiers.LADDER:
            for target in tiers.escalation_path(tier.name):
                self.assertLess(
                    target.rank, tier.rank,
                    f"{tier.name} (rank {tier.rank}) escalates to {target.name} "
                    f"(rank {target.rank}) — not strictly stronger")

    def test_strongest_rung_has_nowhere_to_climb(self):
        self.assertEqual(tiers.escalation_path("opus-5"), [])

    def test_verifier_never_shares_family_with_generator(self):
        """The hard constraint. Same-family verification is weakest exactly
        where it is needed (arXiv:2504.03846)."""
        for tier in tiers.LADDER:
            for verifier in tiers.counter_family(tier.name):
                self.assertNotEqual(verifier.family, tier.family)
                self.assertEqual(verifier.lane, tier.lane)

    def test_a_tier_is_a_surface_model_pair_not_a_model(self):
        """The same model through two surfaces must be two rungs with
        materially different fixed cost, or the ladder hides the real term."""
        thin = tiers.BY_NAME["haiku-4.5"]
        thick = tiers.BY_NAME["cli-haiku-4.5"]
        self.assertNotEqual(thin.surface, thick.surface)
        self.assertGreater(thick.cost.tokens_in, thin.cost.tokens_in * 100)

    def test_cost_weights_change_the_order(self):
        """kirocc meters no dollars. Weighting dollars only there makes every
        rung score zero and the ladder meaningless — this is the guard."""
        by_dollars = [t.name for t in tiers.climb_order("thin", weights={"w_usd": 1})]
        by_seconds = [t.name for t in tiers.climb_order(
            "thin", weights={"w_usd": 0, "w_seconds": 1})]
        self.assertNotEqual(by_dollars, by_seconds)
        self.assertEqual(by_seconds[0], "haiku-4.5")


class TestGate(unittest.TestCase):

    GOOD = {"capability": "c", "instance": "i",
            "claims": [{"statement": "s", "evidence": "https://a.example/p"}],
            "confidence": 0.8}

    def test_a_claim_without_an_evidence_line_is_not_a_claim(self):
        bad = {**self.GOOD, "claims": [{"statement": "s"}]}
        self.assertIn("claims[0] has no evidence line", check_shape(bad))

    def test_unlocatable_evidence_is_rejected_at_the_free_gate(self):
        """Stage 2 verifies evidence and cannot verify what was never cited.
        'trust me' must not survive to cost a verifier call."""
        bad = {**self.GOOD, "claims": [{"statement": "s", "evidence": "trust me"}]}
        self.assertTrue(check_shape(bad))

    def test_locatable_forms_are_accepted(self):
        for evidence in ("https://a.example/x", "runner/gate.py#L42", "src/a.py:17"):
            record = {**self.GOOD, "claims": [{"statement": "s", "evidence": evidence}]}
            self.assertEqual(check_shape(record), [], evidence)

    def test_three_failure_classes_are_distinguished(self):
        """Lumping them together fills the human queue with things no human
        can act on, and a queue that is not actionable does not get read."""
        work = Work("c", "i", "p", "haiku-4.5")
        transport = run_gate(Return(work, None, "", False, "timeout", transport_error=True))
        malformed = run_gate(Return(work, None, "an apology", False, "no json"))
        quality = run_gate(Return(work, {"capability": "c", "instance": "i",
                                         "claims": []}, "", True))
        self.assertEqual(transport.failure_class, "transport")
        self.assertEqual(malformed.failure_class, "malformed")
        self.assertEqual(quality.failure_class, "quality")

    def test_malformed_is_not_retryable(self):
        """There is no correction to hand back: the return was never a record."""
        work = Work("c", "i", "p", "haiku-4.5")
        verdict = run_gate(Return(work, None, "an apology", False, "no json"))
        self.assertFalse(verdict.retryable)

    def test_checkable_gate_takes_a_cheap_rung_judgment_takes_a_strong_one(self):
        cheap = ModelVerifier(CHECKABLE).pick("haiku-4.5")
        strong = ModelVerifier(JUDGMENT).pick("haiku-4.5")
        self.assertEqual(tiers.BY_NAME[cheap].family, "gpt")
        self.assertEqual(tiers.BY_NAME[strong].family, "gpt")
        self.assertLessEqual(tiers.BY_NAME[strong].rank, tiers.BY_NAME[cheap].rank)

    def test_a_verifier_outage_is_not_charged_to_the_generator(self):
        """AGENTS.md invariant 3. The generator returned a clean record; the
        VERIFIER never answered. Filing that as a quality failure depresses the
        generator rung's pass rate over someone else's outage and climbs the
        ladder for a reason that has nothing to do with capability — for as
        long as the outage lasts.

        `ModelVerifier` has carried a comment saying this must not be recorded
        as a quality failure since it was written; before `verifier_transport`
        existed there was no way for that to be true.
        """
        from runner.gate import VerifierResult

        class Outage:
            stage = CHECKABLE

            def __call__(self, ret, **_):
                return VerifierResult(False, ["verifier transport failure: 502"],
                                      True, "copilot-auto", transport=True)

        generator = Return(Work("c", "i", "p", "cli-opus-4.5"), self.GOOD, "", True)
        self.assertFalse(generator.transport_error)
        verdict = run_gate(generator, checkable=Outage())
        self.assertEqual(verdict.failure_class, "transport")

    def test_a_verifier_rejection_on_the_merits_is_still_a_quality_failure(self):
        """The other side of it: a verifier that ANSWERED and said no must keep
        counting, or the fix above would hide every real rejection."""
        from runner.gate import VerifierResult

        class Rejects:
            stage = CHECKABLE

            def __call__(self, ret, **_):
                return VerifierResult(False, ["evidence does not support it"],
                                      True, "copilot-auto")

        generator = Return(Work("c", "i", "p", "cli-opus-4.5"), self.GOOD, "", True)
        self.assertEqual(run_gate(generator, checkable=Rejects()).failure_class,
                         "quality")

    def test_no_verifier_available_fails_closed(self):
        """Passing a record because no verifier was reachable would mark it
        verified on the strength of an outage."""
        verifier = ModelVerifier(CHECKABLE, available=set())
        ok, reasons, retryable, tier, _ = verifier(
            Return(Work("c", "i", "p", "haiku-4.5"), self.GOOD, "", True))
        self.assertFalse(ok)
        self.assertIsNone(tier)
        self.assertTrue(retryable)


class TestCopilotSurface(unittest.TestCase):
    """The thick lane's non-Claude rung, and the two ways adding it could go wrong."""

    def _events(self, *, model="gpt-5.6-luna", content="{}", exit_code=0,
                premium=1):
        import json
        rows = [
            {"type": "session.auto_mode_resolved",
             "data": {"chosenModel": model, "availableModels": [model],
                      "fallback": False}},
            {"type": "assistant.message",
             "data": {"model": model, "content": content,
                      "phase": "final_answer"}},
            {"type": "result", "exitCode": exit_code,
             "usage": {"premiumRequests": premium}},
        ]
        return "\n".join(json.dumps(r) for r in rows)

    def _run(self, stdout, tier="copilot-auto"):
        import types
        from unittest import mock
        work = Work("c", "i", "p", tier)
        dispatcher = dispatch_mod.CopilotDispatcher()
        proc = types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        with mock.patch.object(dispatch_mod.shutil, "which", return_value="/usr/bin/copilot"), \
             mock.patch.object(dispatch_mod.subprocess, "run", return_value=proc):
            return dispatcher._one(work, "p")

    def test_thick_lane_has_a_cross_family_verifier(self):
        """Before this rung, counter_family() was [] for every thick tier, so
        gate 2 failed closed and gate 3 refused. `gate: full` was unreachable
        in the lane where the expensive work happens."""
        for generator in ("cli-opus-4.5", "cli-sonnet-4.5", "cli-haiku-4.5"):
            picked = ModelVerifier(CHECKABLE).pick(generator)
            self.assertIsNotNone(picked, f"gate 2 has no verifier for {generator}")
            self.assertNotEqual(tiers.BY_NAME[picked].family,
                                tiers.BY_NAME[generator].family)
            self.assertEqual(tiers.BY_NAME[picked].lane,
                             tiers.BY_NAME[generator].lane)

    def test_a_thick_rung_does_not_move_the_thin_lane_verifiers(self):
        """A surface added to one lane must not silently repoint the gate in the
        other. A previous rung added with an all-zero cost became the cheapest
        counter-family candidate everywhere and captured gate 2 for the whole
        ladder."""
        for generator in ("haiku-4.5", "sonnet-5", "opus-4.5"):
            picked = ModelVerifier(CHECKABLE).pick(generator)
            self.assertEqual(tiers.BY_NAME[picked].family, "gpt")
            self.assertEqual(tiers.BY_NAME[picked].lane, "thin")

    def test_no_thick_rung_prices_itself_at_zero(self):
        """A rung that costs nothing under every weighting wins every
        comparison, and the ladder stops meaning anything."""
        for tier in tiers.in_lane("thick"):
            self.assertGreater(tier.cost.objective(w_usd=1.0), 0.0, tier.name)

    def test_a_rung_whose_model_is_chosen_per_dispatch_never_generates(self):
        """Price is not a lever for this and must not be used as one.

        copilot-auto is CHEAPER than cli-haiku-4.5 ($0.0400 vs $0.0468) and
        lighter than every claude-cli rung (15,510 tokens vs 22,800+), so it
        sorts first under both the usd and the tokens weighting and the explore
        step routed real work onto it — 52 of 300 dispatches from a
        cli-opus-4.5 floor, each filed under a rung name while some model
        GitHub's router chose did the work. `verifier_only` is what prevents
        that; the price never did.
        """
        import random
        from runner.policy import choose
        for weights in ({"w_usd": 1.0}, {"w_tokens": 1.0}, {"w_seconds": 1.0}):
            names = [t.name for t in tiers.climb_order("thick", weights=weights)]
            self.assertNotIn("copilot-auto", names, weights)
        picked = {choose("newcap", lane="thick", floor_tier="cli-opus-4.5",
                         outcomes=[], rng=random.Random(seed)).tier
                  for seed in range(300)}
        self.assertNotIn("copilot-auto", picked)

    def test_a_verifier_only_rung_is_still_admissible_as_a_verifier(self):
        """The flag must keep it out of generation WITHOUT taking away the one
        thing it was added for."""
        self.assertEqual(ModelVerifier(CHECKABLE).pick("cli-opus-4.5"),
                         "copilot-auto")

    def test_no_escalation_path_climbs_onto_a_verifier_only_rung(self):
        """Escalation is the other door into generation."""
        for tier in tiers.LADDER:
            for target in tiers.escalation_path(tier.name):
                self.assertFalse(target.verifier_only,
                                 f"{tier.name} escalates to {target.name}")

    def test_the_recorded_session_parses(self):
        """Pinned against a REAL copilot stream, not a hand-built one.

        Every other test here builds its own events, which means they all agree
        with whatever this adapter assumed about copilot's schema. This one
        disagrees with it if the assumption was wrong.
        """
        import pathlib as _pathlib
        raw = (_pathlib.Path(__file__).parent / "fixtures"
               / "copilot-session.jsonl").read_text()
        events = dispatch_mod._jsonl(raw)
        self.assertEqual(dispatch_mod._answer_text(events), "OK")
        self.assertEqual(dispatch_mod._answer_model(events), "gpt-5.6-luna")
        self.assertEqual(dispatch_mod._event(events, "result")["exitCode"], 0)
        self.assertEqual(
            dispatch_mod._event(events, "result")["usage"]["premiumRequests"], 1)

    def test_an_untagged_assistant_message_is_still_the_answer(self):
        """`phase` is optional in copilot's schema — only phased-output models
        populate it. Requiring the tag would return "" for every other model,
        and "" is filed `malformed`, which counts against the rung and is never
        retried. The surface would be learned worthless in silence."""
        import json
        rows = [
            {"type": "assistant.message",
             "data": {"model": "mai-code-1.1-flash", "content": '{"a": 1}'}},
            {"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}},
        ]
        ret = self._run("\n".join(json.dumps(r) for r in rows))
        self.assertTrue(ret.ok, ret.error)
        self.assertEqual(ret.record, {"a": 1})

    def test_narration_before_the_answer_is_not_mistaken_for_it(self):
        """The fallback takes the LAST assistant message with content, so a
        JSON-shaped aside on the way to the answer does not become the record."""
        import json
        rows = [
            {"type": "assistant.message",
             "data": {"model": "m", "content": 'let me check {"wrong": true}'}},
            {"type": "assistant.message",
             "data": {"model": "m", "content": '{"right": true}'}},
            {"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}},
        ]
        ret = self._run("\n".join(json.dumps(r) for r in rows))
        self.assertEqual(ret.record, {"right": True})

    def test_an_answer_about_rate_limits_is_not_filed_as_transport(self):
        """The adapter reads copilot's event stream rather than grepping its
        stdout. Copilot writes the answer, the MCP instructions and its errors
        to the same stream, so a substring search for "429" classifies a correct
        answer ABOUT rate limits as an outage — and transport failures are
        excluded from the pass rate, so the loss is silent."""
        answer = ('{"capability":"triage-failures",'
                  '"instance":"HTTP 429: Too Many Requests",'
                  '"claims":[{"statement":"class: substrate",'
                  '"evidence":"ladder/40-runs/x.md:12"}],"confidence":0.9}')
        ret = self._run(self._events(content=answer))
        self.assertFalse(ret.transport_error, ret.error)
        self.assertTrue(ret.ok, ret.error)
        self.assertEqual(ret.record["instance"], "HTTP 429: Too Many Requests")

    def test_a_claude_answer_is_refused_rather_than_counted_as_cross_family(self):
        """This rung is admissible as a thick-lane verifier only because it is
        not Claude family. Copilot's router picks per task and can reach Claude
        models, so a return that came back from one would be same-family
        verification wearing a cross-family rung's name — failing silently and
        reporting success, which is the exact failure counter_family() exists
        to make impossible."""
        ret = self._run(self._events(model="claude-sonnet-4.5"))
        self.assertFalse(ret.ok)
        self.assertIn("claude-sonnet-4.5", ret.error)
        # NOT transport: the dispatch answered and spent a premium request.
        # Transport would be retried clean against a deterministic condition,
        # spend the quota again for the same refusal, and never reach a person.
        self.assertFalse(ret.transport_error)
        self.assertTrue(ret.inadmissible)

    def test_an_unreportable_model_is_refused_too(self):
        """The guard fails closed. `if answered and is_claude(answered)` would
        no-op the moment copilot omitted the field, and the one check holding up
        this rung's admissibility would silently pass everything."""
        import json
        rows = [{"type": "assistant.message", "data": {"content": '{"a":1}'}},
                {"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}}]
        ret = self._run("\n".join(json.dumps(r) for r in rows))
        self.assertFalse(ret.ok)
        self.assertTrue(ret.inadmissible)
        self.assertIn("did not report which model", ret.error)

    def test_an_inadmissible_verifier_reaches_a_human_and_is_not_retried(self):
        """Mirrors gate 3's refusal. If GitHub adds a Claude model to the auto
        router, thick-lane gate 2 stops verifying — that must surface, not be
        retried clean and filed as transport noise where nothing reads it."""
        from runner.gate import VerifierResult

        class Inadmissible:
            stage = CHECKABLE

            def __call__(self, ret, **_):
                return VerifierResult(
                    False, ["verifier inadmissible: copilot routed to "
                            "'claude-sonnet-4.5', which is Claude family"],
                    False, "copilot-auto")

        gen = Return(Work("c", "i", "p", "cli-opus-4.5"),
                     {"capability": "c", "instance": "i",
                      "claims": [{"statement": "s", "evidence": "src/a.py:1"}]},
                     "", True)
        verdict = run_gate(gen, checkable=Inadmissible())
        self.assertFalse(verdict.retryable)
        self.assertNotEqual(verdict.failure_class, "transport")

    def test_input_tokens_cover_the_whole_dispatch_not_the_last_call(self):
        """`lastCallInputTokens` is the tail of a multi-turn dispatch, and the
        run record is the meta-review's only input."""
        summed = dispatch_mod._session_input_tokens({
            "lastCallInputTokens": 400,
            "modelMetrics": {"a": {"usage": {"inputTokens": 15_000}},
                             "b": {"usage": {"inputTokens": 2_000}}}})
        self.assertEqual(summed, 17_000)
        self.assertEqual(
            dispatch_mod._session_input_tokens({"lastCallInputTokens": 400}), 400)

    def test_a_model_the_workspace_has_never_heard_of_is_still_accepted(self):
        """The router names models this ladder does not list — the first live
        dispatch came back from 'mai-code-1.1-flash'. Refusing every unknown id
        would make the surface unusable; the property that must hold is that it
        is not Claude."""
        ret = self._run(self._events(model="mai-code-1.1-flash"))
        self.assertTrue(ret.ok, ret.error)

    def test_the_copilot_rung_can_never_serve_gate_3(self):
        """Its model is decided per dispatch by someone else's router. Gate 3
        refuses rather than seating a judge you cannot name in advance."""
        for generator in ("cli-opus-4.5", "cli-sonnet-4.5", "cli-haiku-4.5"):
            self.assertIsNone(ModelVerifier(JUDGMENT).pick(generator))

    def test_the_premium_request_is_priced_not_discarded(self):
        """Copilot meters premium requests, not dollars. Recording zero would
        make every thick dispatch look free to the policy."""
        ret = self._run(self._events(premium=3))
        self.assertAlmostEqual(
            ret.cost_usd, 3 * dispatch_mod.COPILOT_USD_PER_PREMIUM_REQUEST)


class TestPolicy(unittest.TestCase):

    def test_transport_failures_stay_out_of_the_pass_rate(self):
        """A rate-limit outage that depressed a pass rate would push the policy
        UP the ladder, for a billing reason, for as long as the outage lasted."""
        rows = ([Outcome("c", "haiku-4.5", "transport", "script")] * 30
                + [Outcome("c", "haiku-4.5", "pass", "pass")] * 12)
        cell = tally(rows)[("c", "haiku-4.5")]
        self.assertEqual(cell.trials, 12)
        self.assertEqual(cell.transport, 30)
        self.assertEqual(cell.rate, 1.0)

    def test_malformed_counts_against_the_rung(self):
        """A model that cannot hold the return schema is unfit for the rung."""
        rows = ([Outcome("c", "haiku-4.5", "malformed", "script")] * 6
                + [Outcome("c", "haiku-4.5", "pass", "pass")] * 6)
        self.assertEqual(tally(rows)[("c", "haiku-4.5")].rate, 0.5)

    def test_an_unproven_capability_runs_at_the_declared_floor(self):
        """Never at the cheapest rung: a new capability failing there teaches
        you about the rung, not the capability."""
        decision = choose("brand-new", lane="thin", floor_tier="sonnet-5",
                          outcomes=[], rng=random.Random(0))
        self.assertEqual(decision.tier, "sonnet-5")
        self.assertFalse(decision.exploring)

    def test_evidence_promotes_a_cheaper_rung(self):
        rows = [Outcome("c", "haiku-4.5", "pass", "pass")] * 20
        decision = choose("c", lane="thin", floor_tier="opus-5", outcomes=rows,
                          rng=random.Random(0), weights={"w_usd": 0, "w_seconds": 1})
        self.assertEqual(decision.tier, "haiku-4.5")

    def test_a_failing_cheap_rung_is_not_promoted(self):
        rows = ([Outcome("c", "haiku-4.5", "fail", "checkable")] * 20
                + [Outcome("c", "opus-5", "pass", "pass")] * 20)
        decision = choose("c", lane="thin", floor_tier="opus-5", outcomes=rows,
                          rng=random.Random(0), weights={"w_usd": 0, "w_seconds": 1})
        self.assertNotEqual(decision.tier, "haiku-4.5")

    def test_demotion_needs_no_sample_size_promotion_does(self):
        """Being wrong about 'this cheap rung is fine' costs quality on every
        future run. Being wrong about 'go back up' costs money on a few."""
        few_good = [Outcome("c", "haiku-4.5", "pass", "pass")] * (policy_mod.MIN_SAMPLES - 1)
        decision = choose("c", lane="thin", floor_tier="opus-5", outcomes=few_good,
                          rng=random.Random(99), weights={"w_usd": 0, "w_seconds": 1})
        self.assertEqual(decision.tier, "opus-5", "promoted without a sample size")

        settled = [Outcome("c", "haiku-4.5", "pass", "pass")] * 20
        failing = settled + [Outcome("c", "haiku-4.5", "fail", "checkable")] * 40
        decision = choose("c", lane="thin", floor_tier="opus-5", outcomes=failing,
                          rng=random.Random(99), weights={"w_usd": 0, "w_seconds": 1})
        self.assertNotEqual(decision.tier, "haiku-4.5", "stayed on a failing rung")

    def test_a_dead_surface_is_never_dispatched_to(self):
        rows = [Outcome("c", "haiku-4.5", "pass", "pass")] * 20
        decision = choose("c", lane="thick", floor_tier="cli-opus-4.5",
                          outcomes=rows, rng=random.Random(0),
                          available=set())
        self.assertIn("no rung", decision.why)

    def test_needs_split_reports_a_capability_defined_too_broadly(self):
        """The policy's unit is the capability. That only works if its
        instances are alike."""
        rows = []
        for instance, passes in (("a", True), ("b", True), ("c", False)):
            for _ in range(4):
                rows.append(Outcome("broad", "haiku-4.5",
                                    "pass" if passes else "fail",
                                    "checkable", instance=instance))
        finding = policy_mod.needs_split("broad", rows)
        self.assertIsNotNone(finding)
        self.assertGreaterEqual(finding["spread"], policy_mod.SPLIT_SPREAD)

    def test_exploration_is_the_only_source_of_evidence_for_a_cheaper_rung(self):
        """With exploration off, a rung the policy does not use can never
        acquire the evidence it would need to be used."""
        original = policy_mod.EXPLORE_RATE
        try:
            policy_mod.EXPLORE_RATE = 0.0
            seen = {choose("c", lane="thin", floor_tier="sonnet-5", outcomes=[],
                           rng=random.Random(i),
                           weights={"w_usd": 0, "w_seconds": 1}).tier
                    for i in range(50)}
            self.assertEqual(seen, {"sonnet-5"})
        finally:
            policy_mod.EXPLORE_RATE = original


class TestLearningAcrossRuns(unittest.TestCase):
    """The claim the whole workspace rests on: it gets cheaper as it learns.

    Simulated with a mock whose cheap rung is genuinely good at one capability
    and genuinely bad at another. If the policy cannot tell those apart from
    gate verdicts alone, nothing else here matters.
    """

    def _simulate(self, tier_skill: dict[str, float], runs: int = 14) -> list[str]:
        from runner.run import Stop, execute

        chosen: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            outcomes_path = pathlib.Path(tmp) / "outcomes.jsonl"
            real_outcomes = policy_mod.OUTCOMES
            real_runs, real_returns = None, None
            try:
                policy_mod.OUTCOMES = outcomes_path
                from runner import run as run_mod
                real_runs, real_returns = run_mod.RUNS, run_mod.RETURNS
                run_mod.RUNS = pathlib.Path(tmp) / "runs"
                run_mod.RETURNS = pathlib.Path(tmp) / "returns"
                from runner import gate as gate_mod
                real_queue = gate_mod.NEEDS_HUMAN
                gate_mod.NEEDS_HUMAN = pathlib.Path(tmp) / "needs-human.md"

                for index in range(runs):
                    execute(
                        name=f"sim{index}", capability="task", lane="thin",
                        floor_tier="sonnet-5",
                        instances=[f"i{index}-{n}" for n in range(8)],
                        prompt_for=lambda i: f"do {i}",
                        skill="steps", constraints="none",
                        schema_block="{}", stop=Stop(max_dispatches=40),
                        dispatcher_name="mock", gate_level="script",
                        seed=index,
                        weights={"w_usd": 0, "w_seconds": 1},
                        dispatcher_kwargs={"seed": index, "tier_skill": tier_skill},
                    )
                    decision = policy_mod.choose(
                        "task", lane="thin", floor_tier="sonnet-5",
                        outcomes=policy_mod.load(outcomes_path),
                        rng=random.Random(0),
                        weights={"w_usd": 0, "w_seconds": 1})
                    chosen.append(decision.tier)
                gate_mod.NEEDS_HUMAN = real_queue
            finally:
                policy_mod.OUTCOMES = real_outcomes
                if real_runs is not None:
                    from runner import run as run_mod
                    run_mod.RUNS, run_mod.RETURNS = real_runs, real_returns
        return chosen

    def test_it_settles_cheap_when_the_cheap_rung_is_good_enough(self):
        chosen = self._simulate({"haiku-4.5": 0.99, "gpt-5.6-sol": 0.99,
                                 "gpt-5.6-luna": 0.99, "gpt-5.6-terra": 0.99,
                                 "sonnet-5": 0.99, "opus-5": 0.99, "opus-4.5": 0.99})
        self.assertNotEqual(chosen[-1], "sonnet-5",
                            f"never left the floor tier: {chosen}")
        self.assertLess(tiers.BY_NAME[chosen[-1]].cost.seconds,
                        tiers.BY_NAME["sonnet-5"].cost.seconds,
                        f"settled on something slower than the floor: {chosen}")

    def test_it_refuses_to_go_cheap_when_the_cheap_rung_is_bad(self):
        chosen = self._simulate({"haiku-4.5": 0.20, "gpt-5.6-luna": 0.20,
                                 "gpt-5.6-terra": 0.25, "gpt-5.6-sol": 0.30,
                                 "sonnet-5": 0.98, "opus-5": 0.99, "opus-4.5": 0.99})
        self.assertNotEqual(chosen[-1], "haiku-4.5",
                            f"settled on a rung that fails 80% of the time: {chosen}")


class TestEvidenceKinds(unittest.TestCase):
    """Evidence is a locator or a verifiable quotation. Nothing else."""

    SOURCE = "A dispatch failed. The reason was: HTTP 429: Too Many Requests."

    def _record(self, evidence):
        return {"capability": "c", "instance": "i",
                "claims": [{"statement": "s", "evidence": evidence}]}

    def test_a_real_quotation_is_accepted(self):
        self.assertEqual(
            check_shape(self._record("HTTP 429: Too Many Requests"),
                        source_text=self.SOURCE), [])

    def test_a_fabricated_quotation_is_rejected_for_free(self):
        """The check that would cost a verifier call, done by a regex."""
        errors = check_shape(self._record("HTTP 503 Service Unavailable"),
                             source_text=self.SOURCE)
        self.assertTrue(any("does not appear in the source" in e for e in errors))

    def test_a_rewrapped_quotation_is_still_a_quotation(self):
        """Rejecting reflowed whitespace would push agents toward shorter,
        less useful citations."""
        self.assertEqual(
            check_shape(self._record("HTTP 429:\n   Too Many    Requests"),
                        source_text=self.SOURCE), [])

    def test_a_quotation_with_no_source_cannot_be_verified_so_is_refused(self):
        """Unverifiable evidence is how a citation becomes a decoration."""
        self.assertTrue(check_shape(self._record("HTTP 429: Too Many Requests")))

    def test_a_locator_needs_no_source(self):
        for evidence in ("https://a.example/x", "runner/gate.py#L42", "src/a.py:17"):
            self.assertEqual(check_shape(self._record(evidence)), [], evidence)


class TestLint(unittest.TestCase):
    """Each of these is a defect that reached a live run before it was a rule."""

    def _launch(self, text):
        from runner.launch import parse
        return parse(text, "probe")

    HEAD = ("---\ncapability: probe\nquestion: q\nlane: {lane}\n"
            "floor_tier: {floor}\ngate: {gate}\nmax_attempts: {attempts}\n"
            "max_dispatches: 20\n---\n\n## prompt\n\nDo {{instance}}.\n\n"
            "## return\n\n{ret}\n\n## instances\n\n- alpha\n")

    def test_a_launch_whose_own_example_fails_gate_1_is_caught(self):
        """The defect that cost the first live run: a launch asking for
        evidence its own gate rejects."""
        from runner.lint import lint
        text = self.HEAD.format(
            lane="thin", floor="sonnet-5", gate="script", attempts=2,
            ret='{"capability":"probe","instance":"x","claims":[{"statement":"s"}]}')
        findings = lint(self._launch(text))
        self.assertTrue(any("own example return fails gate 1" in f for f in findings))

    def test_a_retry_with_nowhere_to_climb_is_caught(self):
        """Escalating from the top rung re-runs the same rung: a re-roll, not
        a correction."""
        from runner.lint import lint
        text = self.HEAD.format(
            lane="thin", floor="opus-5", gate="script", attempts=2,
            ret='{"capability":"probe","instance":"x","claims":'
                '[{"statement":"s","evidence":"https://a.example/x"}]}')
        findings = lint(self._launch(text))
        self.assertTrue(any("nowhere to climb" in f for f in findings))

    def test_gate_3_with_no_peer_or_stronger_judge_is_caught(self):
        """`gate: full` at a floor with no peer-or-stronger rung of another
        family sends every return to a human. The thick lane's only non-Claude
        rung is `copilot-auto` at rank 5, so a cli-sonnet-4.5 floor (rank 2)
        has a gate 2 verifier and no gate 3 judge — and a check that only asked
        whether ANY counter-family rung exists would report this launch clean.

        Named for gate 3 because that is the branch it exercises: no tier in
        LADDER has an empty `counter_family()` any more, so the gate 2 branch
        is unreachable from a real ladder and a test claiming to cover it would
        be claiming coverage it does not have. The assertion names the gate 3
        message specifically rather than the substring both messages share."""
        from runner.lint import lint
        text = self.HEAD.format(
            lane="thick", floor="cli-sonnet-4.5", gate="full", attempts=1,
            ret='{"capability":"probe","instance":"x","claims":'
                '[{"statement":"s","evidence":"src/a.py:1"}]}')
        findings = lint(self._launch(text))
        self.assertTrue(any("gate 3 refuses" in f for f in findings), findings)
        self.assertFalse(any("fail closed at gate 2" in f for f in findings),
                         findings)

    def test_gate_3_is_checked_at_the_rungs_a_retry_escalates_to(self):
        """A launch is not run only at its floor. haiku-4.5 (rank 4) has gpt
        judges above it, so the floor lints clean — but with max_attempts 2 the
        first quality retry climbs to rank 0/1 where gate 3 refuses and every
        record goes to a human. Checking the floor alone reports clean and
        discovers it one retry later, at full price."""
        from runner.lint import lint
        text = self.HEAD.format(
            lane="thin", floor="haiku-4.5", gate="full", attempts=2,
            ret='{"capability":"probe","instance":"x","claims":'
                '[{"statement":"s","evidence":"src/a.py:1"}]}')
        findings = lint(self._launch(text))
        self.assertTrue(any("escalation" in f and "gate 3 refuses" in f
                            for f in findings), findings)

    def test_a_verifier_on_a_dead_surface_is_not_a_verifier(self):
        """`ModelVerifier.pick` filters candidates by the surfaces the probe
        found live; a linter that does not is answering a different question
        from the one the run will ask. The thick lane's entire gate hangs on one
        optional third-party binary, and the cheapest check in the workspace has
        to say so before a run spends anything."""
        import tempfile as _tf, json as _json, pathlib as _pl
        from runner import substrate as _sub
        from runner.lint import lint
        text = self.HEAD.format(
            lane="thick", floor="cli-sonnet-4.5", gate="checkable", attempts=1,
            ret='{"capability":"probe","instance":"x","claims":'
                '[{"statement":"s","evidence":"src/a.py:1"}]}')
        with _tf.TemporaryDirectory() as tmp:
            health = _pl.Path(tmp) / "substrate.json"
            health.write_text(_json.dumps({"checked": "now", "surfaces": [
                {"name": "claude-cli", "available": True},
                {"name": "copilot", "available": False}]}))
            with unittest.mock.patch.object(_sub, "HEALTH", health):
                findings = lint(self._launch(text))
        self.assertTrue(any("did not find live" in f for f in findings), findings)

    def test_gate_full_is_clean_where_a_peer_or_stronger_judge_exists(self):
        """The other half of the rule, so it does not become a blanket refusal.
        In the thin lane a haiku-4.5 floor (rank 4) has gpt rungs at rank 2
        above it, so gate 3 has an admissible judge and the launch must NOT be
        flagged."""
        from runner.lint import lint
        text = self.HEAD.format(
            lane="thin", floor="haiku-4.5", gate="full", attempts=1,
            ret='{"capability":"probe","instance":"x","claims":'
                '[{"statement":"s","evidence":"src/a.py:1"}]}')
        findings = lint(self._launch(text))
        self.assertFalse([f for f in findings if "model family" in f], findings)

    def test_gate_full_in_the_thick_lane_is_flagged_at_every_floor(self):
        """The thick lane's only non-Claude rung is copilot, whose model is
        chosen per dispatch by someone else's router. It is rank 5 on purpose,
        so no thick floor has a peer-or-stronger judge and `gate: full` there
        still sends every return to a human. The linter has to say so before
        the run, not after."""
        from runner.lint import lint
        for floor in ("cli-opus-4.5", "cli-sonnet-4.5", "cli-haiku-4.5"):
            text = self.HEAD.format(
                lane="thick", floor=floor, gate="full", attempts=1,
                ret='{"capability":"probe","instance":"x","claims":'
                    '[{"statement":"s","evidence":"src/a.py:1"}]}')
            findings = lint(self._launch(text))
            self.assertTrue([f for f in findings if "model family" in f],
                            f"{floor}: {findings}")

    def test_the_shipped_launch_lints_clean(self):
        from runner.launch import load_launch
        from runner.lint import lint
        self.assertEqual(lint(load_launch("triage-failures")), [])


class TestLaunchValidation(unittest.TestCase):

    def test_a_floor_tier_outside_its_lane_is_rejected(self):
        """Selection would find nothing and report a clean, empty success."""
        from runner.launch import Launch
        launch = Launch(name="x", capability="x", question="q", lane="thin",
                        floor_tier="cli-opus-4.5", instances=["a"],
                        prompt_template="{instance}")
        self.assertTrue(any("lane" in e for e in launch.validate()))

    def test_irreversible_work_requires_the_full_gate(self):
        from runner.launch import Launch
        launch = Launch(name="x", capability="x", question="q", lane="thin",
                        floor_tier="opus-5", risk_class="irreversible",
                        gate="script", instances=["a"], prompt_template="{instance}")
        self.assertTrue(any("irreversible" in e for e in launch.validate()))

    def test_irreversible_work_never_explores_a_cheaper_rung(self):
        """Exploration is a controlled experiment, and an experiment you cannot
        undo is not controlled."""
        rows = [Outcome("c", "haiku-4.5", "pass", "pass")] * 40
        seen = {choose("c", lane="thin", floor_tier="opus-5", outcomes=rows,
                       rng=random.Random(i), explore=False,
                       weights={"w_usd": 0, "w_seconds": 1}).exploring
                for i in range(40)}
        self.assertEqual(seen, {False})


class TestNoWeakJudgeOnStrongWork(unittest.TestCase):
    """Gate 3 refuses a weaker verifier rather than falling back to one.

    Verification skill tracks the verifier's own generation ability, and errors
    from a STRONGER generator are the hardest to detect because they are
    internally consistent and wrong (arXiv:2509.17995). A weaker judge does not
    give a weaker gate; it gives a gate that passes exactly what it was
    installed to catch.
    """

    def test_the_top_rung_has_no_admissible_judgment_verifier(self):
        self.assertIsNone(ModelVerifier(JUDGMENT).pick("opus-5"))

    def test_a_weaker_cross_family_rung_is_never_chosen_for_judgment(self):
        for tier in tiers.LADDER:
            chosen = ModelVerifier(JUDGMENT).pick(tier.name)
            if chosen is not None:
                self.assertLessEqual(
                    tiers.BY_NAME[chosen].rank, tier.rank,
                    f"gate 3 put {chosen} (rank {tiers.BY_NAME[chosen].rank}) "
                    f"on {tier.name} (rank {tier.rank})")

    def test_refusing_routes_to_a_human_and_is_not_retried(self):
        """Retrying changes nothing: no rung on the ladder may judge this."""
        ok, reasons, retryable, tier, _ = ModelVerifier(JUDGMENT)(
            Return(Work("c", "i", "p", "opus-5"),
                   {"capability": "c", "instance": "i",
                    "claims": [{"statement": "s", "evidence": "https://a.example/x"}]},
                   "", True))
        self.assertFalse(ok)
        self.assertFalse(retryable)
        self.assertIsNone(tier)

    def test_gate_2_may_still_use_a_cheap_rung_on_the_top_rung(self):
        """Checkable claims do not need strength — that is the whole point of
        splitting stage 2 from stage 3."""
        self.assertIsNotNone(ModelVerifier(CHECKABLE).pick("opus-5"))


class TestNoFakeCompletion(unittest.TestCase):
    """Frontier models cheat on repo tasks at ~49-54% (arXiv:2510.20270), and
    most exploits are syntactically visible — so they are caught for free."""

    def _record(self, statement):
        return {"capability": "c", "instance": "i",
                "claims": [{"statement": statement,
                            "evidence": "https://a.example/x"}]}

    def test_placeholder_markers_are_rejected(self):
        from runner.gate import check_no_fake_completion
        for marker in ("TODO: finish this", "added test.skip for now",
                       "raise NotImplementedError", "FIXME later",
                       "a stub implementation", "will implement later"):
            self.assertTrue(check_no_fake_completion(self._record(marker)),
                            f"not caught: {marker}")

    def test_honest_work_passes(self):
        from runner.gate import check_no_fake_completion
        self.assertEqual(
            check_no_fake_completion(self._record("replaced the loop with a map")),
            [])

    def test_it_runs_inside_gate_1(self):
        errors = check_shape(self._record("TODO: implement the branch"))
        self.assertTrue(any("placeholder" in e for e in errors))

    def test_artifact_text_is_scanned_too(self):
        from runner.gate import check_no_fake_completion
        self.assertTrue(check_no_fake_completion(
            self._record("implemented the parser"),
            artifact_text="def parse():\n    raise NotImplementedError"))


class TestLowConfidenceDeferral(unittest.TestCase):
    """The one admissible use of self-reported confidence: low escalates,
    high means nothing (arXiv:2412.14737, arXiv:2604.01457)."""

    def _ret(self, confidence):
        return Return(Work("c", "i", "p", "haiku-4.5"),
                      {"capability": "c", "instance": "i",
                       "claims": [{"statement": "s",
                                   "evidence": "https://a.example/x"}],
                       "confidence": confidence}, "", True)

    def test_low_confidence_defers(self):
        verdict = run_gate(self._ret(0.3))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.failure_class, "deferred")

    def test_high_confidence_grants_nothing_extra(self):
        """A confident answer passes because it passed the gates, not because
        it was confident."""
        self.assertTrue(run_gate(self._ret(0.99)).ok)

    def test_high_confidence_cannot_rescue_a_failing_record(self):
        bad = Return(Work("c", "i", "p", "haiku-4.5"),
                     {"capability": "c", "instance": "i", "claims": [],
                      "confidence": 1.0}, "", True)
        self.assertFalse(run_gate(bad).ok)

    def test_deferral_is_not_counted_against_the_rung(self):
        """Punishing an honest low-confidence signal would train the next model
        to overclaim — the one failure this gate cannot detect."""
        rows = ([Outcome("c", "haiku-4.5", "deferred", "deferred")] * 20
                + [Outcome("c", "haiku-4.5", "pass", "pass")] * 12)
        cell = tally(rows)[("c", "haiku-4.5")]
        self.assertEqual(cell.trials, 12)
        self.assertEqual(cell.rate, 1.0)


class TestNoModuleLevelPathDefaults(unittest.TestCase):
    """Every writer resolves its path at CALL time, not at import.

    A module-level default freezes the path when the module is imported, so a
    caller that redirects it writes to the real file while believing it wrote to
    its own. This is not a test-only nuisance: it silently appended simulated
    failures to the real `needs-human.md`, which is the one file a human writes
    decisions in. A queue full of things that never happened is a log, and logs
    do not get read.
    """

    WRITERS = [("runner.gate", "escalate"),
               ("runner.policy", "record"),
               ("runner.policy", "load"),
               ("runner.substrate", "write_health"),
               ("runner.substrate", "read_health"),
               ("runner.substrate", "available")]

    def test_no_writer_binds_its_path_at_import(self):
        import importlib
        import inspect
        for module_name, function_name in self.WRITERS:
            module = importlib.import_module(module_name)
            parameter = inspect.signature(
                getattr(module, function_name)).parameters.get("path")
            if parameter is None:
                continue
            self.assertIsNone(
                parameter.default,
                f"{module_name}.{function_name} binds `path` at import "
                f"({parameter.default!r}); redirecting the module global will "
                f"not affect it")

    def test_escalate_honours_a_redirected_module_global(self):
        from runner import gate as gate_mod
        with tempfile.TemporaryDirectory() as tmp:
            redirected = pathlib.Path(tmp) / "needs-human.md"
            real = gate_mod.NEEDS_HUMAN
            try:
                gate_mod.NEEDS_HUMAN = redirected
                verdict = run_gate(Return(
                    Work("c", "i", "p", "haiku-4.5"),
                    {"capability": "c", "instance": "i", "claims": []}, "", True))
                written = gate_mod.escalate([verdict], "test-run")
            finally:
                gate_mod.NEEDS_HUMAN = real
            self.assertEqual(written, 1)
            self.assertTrue(redirected.exists(),
                            "escalate wrote somewhere other than the redirect")
            self.assertIn("test-run", redirected.read_text())


class TestDeferralReachesTheHumanQueueAtTheTop(unittest.TestCase):
    """A record still unsure on the strongest rung is a person's decision."""

    def test_deferral_at_the_top_of_the_ladder_is_queued(self):
        from runner import gate as gate_mod
        with tempfile.TemporaryDirectory() as tmp:
            redirected = pathlib.Path(tmp) / "needs-human.md"
            real = gate_mod.NEEDS_HUMAN
            try:
                gate_mod.NEEDS_HUMAN = redirected
                verdict = run_gate(Return(
                    Work("c", "i", "p", "opus-5"),
                    {"capability": "c", "instance": "i",
                     "claims": [{"statement": "s", "evidence": "https://a.example/x"}],
                     "confidence": 0.2}, "", True))
                self.assertEqual(verdict.failure_class, "deferred")
                self.assertEqual(gate_mod.escalate([verdict], "top"), 1)
            finally:
                gate_mod.NEEDS_HUMAN = real
            self.assertIn("confidence", redirected.read_text())


class TestLaneAwareAvailability(unittest.TestCase):
    """A live surface somewhere is not a live surface in YOUR lane.

    Checking only "is anything up" dispatches a thin-lane launch into a dead
    proxy and files every 502 as a transport failure — a whole sweep spent
    rediscovering what the probe already recorded.
    """

    def test_choose_refuses_when_the_lane_has_no_live_surface(self):
        decision = choose("c", lane="thin", floor_tier="sonnet-5", outcomes=[],
                          rng=random.Random(0), available={"claude-cli"})
        self.assertIn("no rung", decision.why)

    def test_a_lane_with_a_live_surface_still_selects(self):
        decision = choose("c", lane="thick", floor_tier="cli-sonnet-4.5",
                          outcomes=[], rng=random.Random(0),
                          available={"claude-cli"})
        self.assertEqual(tiers.BY_NAME[decision.tier].surface, "claude-cli")

    def test_lanes_do_not_share_surfaces(self):
        """If they ever did, the check above would silently stop meaning
        anything."""
        thin = {t.surface for t in tiers.in_lane("thin")}
        thick = {t.surface for t in tiers.in_lane("thick")}
        self.assertEqual(thin & thick, set())


class TestCleanKeepsDocumentation(unittest.TestCase):
    """`ladder clean` removes generated artifacts and never a README.

    The shell one-liner this replaces — `rm -rf 40-runs/*.md 10-returns/*` —
    also deleted the README documenting each directory, and `git add -A` then
    staged the deletion silently. It happened twice in this repo before anyone
    noticed, which is exactly how a directory ends up unexplained.
    """

    def test_clean_spares_readmes_and_removes_the_rest(self):
        from runner.workspace import clean
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for directory in ("10-returns", "40-runs", "20-graph", "30-queries"):
                (root / directory).mkdir()
                (root / directory / "README.md").write_text("# docs\n")
            (root / "10-returns" / "run-1").mkdir()
            (root / "10-returns" / "run-1" / "a.json").write_text("{}")
            (root / "40-runs" / "run-1.md").write_text("# run\n")
            (root / "20-graph" / "outcomes.jsonl").write_text("{}\n")
            (root / "30-queries" / "needs-human.md").write_text("# queue\n")

            removed = clean(root)

            for directory in ("10-returns", "40-runs", "20-graph", "30-queries"):
                self.assertTrue((root / directory / "README.md").exists(),
                                f"clean deleted {directory}/README.md")
            self.assertFalse((root / "10-returns" / "run-1").exists())
            self.assertFalse((root / "40-runs" / "run-1.md").exists())
            self.assertFalse((root / "20-graph" / "outcomes.jsonl").exists())
            self.assertFalse((root / "30-queries" / "needs-human.md").exists())
            self.assertEqual(len(removed), 4)

    def test_clean_resolves_its_root_at_call_time(self):
        """A module-level default would bind at import and clean the real
        workspace instead — the same defect already found in gate.escalate()."""
        import inspect
        from runner.workspace import clean
        self.assertIsNone(inspect.signature(clean).parameters["root"].default)

    def test_clean_is_idempotent(self):
        from runner.workspace import clean
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(clean(pathlib.Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
