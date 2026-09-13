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

    def test_no_verifier_available_fails_closed(self):
        """Passing a record because no verifier was reachable would mark it
        verified on the strength of an outage."""
        verifier = ModelVerifier(CHECKABLE, available=set())
        ok, reasons, retryable, tier = verifier(
            Return(Work("c", "i", "p", "haiku-4.5"), self.GOOD, "", True))
        self.assertFalse(ok)
        self.assertIsNone(tier)
        self.assertTrue(retryable)


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

    def test_a_gate_with_no_cross_family_rung_is_caught(self):
        """The thick lane holds only Claude rungs, so `gate: full` there would
        fail closed on every return."""
        from runner.lint import lint
        text = self.HEAD.format(
            lane="thick", floor="cli-sonnet-4.5", gate="full", attempts=1,
            ret='{"capability":"probe","instance":"x","claims":'
                '[{"statement":"s","evidence":"src/a.py:1"}]}')
        findings = lint(self._launch(text))
        self.assertTrue(any("different model family" in f for f in findings))

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
        ok, reasons, retryable, tier = ModelVerifier(JUDGMENT)(
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


if __name__ == "__main__":
    unittest.main()
