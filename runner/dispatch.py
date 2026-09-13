"""Dispatch: send one unit of work to one rung, get a record back.

Three adapters, and the difference between the first two is the whole reason
this workspace exists.

  thin   `KiroccDispatcher` — one HTTP POST to the local Anthropic-compatible
         proxy. ~91 tokens of overhead. No tools, no filesystem, no repo.
  thick  `ClaudeCliDispatcher` — one `claude -p` subprocess. ~22,800 tokens of
         overhead before your prompt is read. Tools, filesystem, repo context.
  mock   `MockDispatcher` — synthetic returns with a deliberate defect rate, so
         the gate, the retry, the escalation and the policy can all be
         exercised end to end without spending anything.

The mock is not a testing afterthought. It is the default, because a broken
launch file should be caught by a run that costs nothing rather than by three
hundred agents that cost something.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import pathlib
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from .substrate import KIROCC_BASE, _event, _jsonl
from .tiers import BY_NAME, Tier

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE = re.compile(r"(\{.*\})", re.DOTALL)

# Tools a thick-lane worker may hold. The deny list matters more than the allow
# list: `--restricted` would look right and is wrong, because it strips WebFetch
# and an allow list cannot put back what restricted mode removed.
THICK_ALLOWED = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "ToolSearch"]
THICK_DENIED = ["Bash", "Edit", "Write", "NotebookEdit", "Agent", "Skill"]

# WebSearch and WebFetch are deferred in Claude Code — present but not loaded
# until asked for. An agent that does not know this concludes it has no research
# tools and returns an apology instead of a record.
DEFERRED_TOOL_NOTE = (
    "Some tools in this environment are deferred: present, but not loaded until "
    "requested. If you need web access, call ToolSearch with the query "
    "`select:WebSearch,WebFetch` before anything else."
)


@dataclasses.dataclass
class Work:
    """One unit of work: a capability applied to one concrete instance."""

    capability: str
    instance: str
    prompt: str
    tier: str
    attempt: int = 1
    failure_reason: str | None = None
    exploring: bool = False


@dataclasses.dataclass
class Return:
    """What came back. `record` is None whenever nothing usable arrived."""

    work: Work
    record: dict | None
    raw: str
    ok: bool
    error: str | None = None
    seconds: float = 0.0
    cost_usd: float = 0.0
    tokens_in: int = 0
    # True when the agent never got to answer: timeout, non-zero exit, 401,
    # 429, missing CLI. Not evidence the work was bad, and it must never be
    # filed next to work that failed on its merits.
    transport_error: bool = False
    # True when the dispatch SUCCEEDED but the rung that answered is not one
    # this work may be recorded against — today, a verifier surface that routed
    # to the generator's own model family. Distinct from `transport_error`
    # because it is deterministic: retrying reproduces it, spends the surface's
    # quota again and answers nothing. It must reach a person, the way gate 3's
    # refusal does, rather than being retried clean and filed as noise.
    inadmissible: bool = False


def _final_message(events: list[dict]) -> dict:
    """The assistant message carrying the answer, as a `data` payload.

    Copilot tags the answering message `phase: "final_answer"` — verified in a
    recorded stream, see tests/fixtures/copilot-session.jsonl. But `phase` is
    OPTIONAL in copilot's own schema ("generation phase for phased-output
    models"), so a model that does not phase its output emits no tag at all.

    Keying only on the tag would return "" for such a model, and "" is not a
    loud failure: `extract_json` yields None, the verdict is `malformed`, and
    SCHEMA.md says malformed counts against the rung and is never retried. The
    surface would be learned to be worthless without one error being raised.

    So the tag is a preference, not a requirement, and the fallback is the last
    assistant message that actually said something. Taking the LAST rather than
    concatenating is what keeps tool-call narration out of the record.
    """
    fallback = {}
    for event in reversed(events):
        if event.get("type") != "assistant.message":
            continue
        data = event.get("data") or {}
        if data.get("phase") == "final_answer":
            return data
        if not fallback and (data.get("content") or "").strip():
            fallback = data
    return fallback


def _answer_text(events: list[dict]) -> str:
    return _final_message(events).get("content") or ""


def _answer_model(events: list[dict]) -> str | None:
    """Which model produced the answer, per copilot's own event."""
    return _final_message(events).get("model")


def _session_input_tokens(usage: dict) -> int:
    """Input tokens for the WHOLE dispatch, summed across every model it used.

    `lastCallInputTokens` names exactly what it says — the most recent
    main-agent API call. A thick-lane dispatch is multi-turn by definition, so
    on any dispatch that used a tool that field is the tail of the work and not
    the work, and it under-reports into the run record silently. `modelMetrics`
    is per-model totals for the session, which is the quantity the audit trail
    is asking for.
    """
    metrics = usage.get("modelMetrics")
    if isinstance(metrics, dict):
        total = sum((entry.get("usage") or {}).get("inputTokens", 0) or 0
                    for entry in metrics.values() if isinstance(entry, dict))
        if total:
            return total
    return usage.get("lastCallInputTokens", 0) or 0


def _is_claude_family(model: str) -> bool:
    """Whether copilot's router handed back an Anthropic model.

    Matched on the id rather than a lookup table, because the router may name a
    model this workspace has never heard of and the safe default for an unknown
    id is "not Claude, but check the run record". Anthropic ids have carried
    `claude` in them on every surface probed here.
    """
    return "claude" in (model or "").lower()


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def extract_json(text: str) -> dict | None:
    for pattern in (_FENCE, _BARE):
        match = pattern.search(text or "")
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    return None


def build_prompt(work: Work, *, skill: str, constraints: str,
                 schema_block: str, lane: str) -> str:
    """Assemble the prompt. Procedure from files, never from the caller's head.

    A prompt is a sample of intent; a file is the intent itself. SKILL.md and
    CONSTRAINTS.md are loaded here so that changing how the system works is a
    diff someone reviews, not a string someone retyped.
    """
    parts = []
    if lane == "thick":
        parts.append(DEFERRED_TOOL_NOTE)
    parts += [
        "# CONSTRAINTS — these override everything below",
        constraints.strip(),
        "# PROCEDURE",
        skill.strip(),
        "# TASK",
        f"capability: {work.capability}",
        f"instance: {work.instance}",
        work.prompt.strip(),
    ]
    if work.failure_reason:
        parts.append(
            "# YOUR PREVIOUS ATTEMPT FAILED THE GATE\n"
            f"{work.failure_reason}\n"
            "Correct exactly this. Retrying without changing it will fail again."
        )
    parts += [
        "# RETURN",
        schema_block.strip(),
        "Return that JSON object and nothing else. No preamble, no explanation, "
        "no markdown outside the JSON.",
    ]
    return "\n\n".join(parts)


class KiroccDispatcher:
    """The thin lane. One HTTP POST, ~91 tokens of overhead, no tools.

    This is where cheap rungs actually pay for themselves, because there is
    almost nothing to pay for besides the answer.
    """

    name = "kirocc"
    lane = "thin"

    # Codes worth waiting out rather than giving up on. 429 is an explicit rate
    # limit; 502/503/504 are the proxy's upstream refusing, which on this
    # machine is what a burst of parallel dispatches produces. Learned the hard
    # way: a 16-way fan-out took the proxy to 502 for every request, and
    # without backoff the run's one clean retry fired into the same wall and
    # the whole sweep came back transport-failed in 1.2 seconds.
    RETRYABLE = (429, 500, 502, 503, 504)

    def __init__(self, concurrency: int = 4, timeout: int = 180,
                 max_tokens: int = 4096, backoff_attempts: int = 4,
                 backoff_base: float = 2.0):
        # Default concurrency is deliberately modest. The bottleneck here is
        # not local; it is whatever the proxy is in front of, and that is a
        # shared quota rather than a machine you can make bigger.
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.backoff_attempts = backoff_attempts
        self.backoff_base = backoff_base

    def _one(self, work: Work, prompt: str) -> Return:
        tier: Tier = BY_NAME[work.tier]
        payload = {
            "model": tier.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            f"{KIROCC_BASE}/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json",
                     # /v1/messages rejects requests without this header.
                     "X-Claude-Code-Session-Id": f"ladder-{work.capability}"},
            method="POST",
        )
        started = time.monotonic()
        body = None
        last_error = "no attempt was made"
        for attempt in range(self.backoff_attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode())
                break
            except urllib.error.HTTPError as exc:
                # 401 and 429 both mean the model never saw the task, so both
                # are transport. Filing them as quality failures would push the
                # policy up the ladder for a billing problem.
                last_error = f"HTTP {exc.code}: {exc.reason}"
                if exc.code not in self.RETRYABLE:
                    break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.backoff_attempts - 1:
                # Jittered exponential backoff. The jitter matters more than
                # the exponent: without it a fan-out that tripped a limit
                # together retries together and trips it again.
                delay = self.backoff_base ** attempt
                time.sleep(delay + random.random() * delay)

        if body is None:
            return Return(work, None, "", False,
                          f"{last_error} (after {self.backoff_attempts} attempts)",
                          time.monotonic() - started, transport_error=True)

        seconds = time.monotonic() - started
        usage = body.get("usage", {}) or {}
        tokens_in = usage.get("input_tokens", 0)
        text = "".join(block.get("text", "") for block in body.get("content", []))
        record = extract_json(text)
        if record is None:
            # It answered, just not in the schema. That is a malformed return,
            # not a transport failure, and it counts against the rung.
            return Return(work, None, text, False, "no json object in the reply",
                          seconds, 0.0, tokens_in)
        return Return(work, record, text, True, None, seconds, 0.0, tokens_in)

    def run(self, works: list[tuple[Work, str]]) -> list[Return]:
        return _pool(self._one, works, self.concurrency)


class ClaudeCliDispatcher:
    """The thick lane. One `claude -p` subprocess, ~22,800 tokens of overhead.

    Reserve it for work that genuinely needs tools and repo context. The
    overhead is fixed across models, so routing a classification job here and
    then picking a cheap model saves 4.9x on a bill whose floor is 100%
    harness — the wrong lever, pulled hard.
    """

    name = "claude-cli"
    lane = "thick"

    def __init__(self, concurrency: int = 4, timeout: int = 600,
                 cwd: str | None = None):
        self.concurrency = concurrency
        self.timeout = timeout
        self.cwd = cwd

    def _command(self, prompt: str, model: str) -> list[str]:
        return [
            "claude", "-p", prompt, "--output-format", "json", "--model", model,
            "--allowed-tools", *THICK_ALLOWED,
            "--disallowed-tools", *THICK_DENIED,
            # An unattended agent has no approval surface. The allow list above
            # is what keeps deny-everything from denying its own tools.
            "--permission-prompts", "none",
        ]

    def _one(self, work: Work, prompt: str) -> Return:
        tier: Tier = BY_NAME[work.tier]
        if shutil.which("claude") is None:
            return Return(work, None, "", False, "the `claude` CLI is not on PATH",
                          0.0, transport_error=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(self._command(prompt, tier.model),
                                  capture_output=True, text=True,
                                  timeout=self.timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired:
            return Return(work, None, "", False, f"timed out after {self.timeout}s",
                          time.monotonic() - started, transport_error=True)
        except Exception as exc:                              # noqa: BLE001
            # OSError from fork exhaustion, UnicodeDecodeError on stderr,
            # anything else. Re-raising here would propagate out of
            # future.result() and discard every sibling's paid-for output.
            return Return(work, None, "", False, f"{type(exc).__name__}: {exc}",
                          time.monotonic() - started, transport_error=True)

        seconds = time.monotonic() - started
        if proc.returncode != 0:
            return Return(work, None, proc.stdout, False,
                          f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}",
                          seconds, transport_error=True)
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return Return(work, None, proc.stdout, False, "claude did not return json",
                          seconds, transport_error=True)

        cost = envelope.get("total_cost_usd", 0.0)
        usage = envelope.get("usage", {}) or {}
        tokens_in = (usage.get("input_tokens", 0)
                     + usage.get("cache_creation_input_tokens", 0)
                     + usage.get("cache_read_input_tokens", 0))
        text = envelope.get("result", "")
        record = extract_json(text)
        if record is None:
            return Return(work, None, text, False, "no json object in the reply",
                          seconds, cost, tokens_in)
        return Return(work, record, text, True, None, seconds, cost, tokens_in)

    def run(self, works: list[tuple[Work, str]]) -> list[Return]:
        return _pool(self._one, works, self.concurrency)


class MockDispatcher:
    """Synthetic returns. Exercises the pipeline, proves nothing about the world.

    `defect_rate` decides how many returns are deliberately malformed so the
    gate and the retry path get tested rather than only the happy path.
    `stubborn` is the slice that stays broken on every attempt, which is the
    only way to reach the retry cap and the escalation to the human queue.

    `tier_skill` is what makes this useful for testing the *policy* rather than
    only the plumbing: it maps a tier name to the probability its returns are
    clean, so a cheap rung can be made genuinely worse than an expensive one
    and the promote/demote logic has something real to learn from.
    """

    name = "mock"
    lane = "thin"

    def __init__(self, defect_rate: float = 0.25, stubborn_rate: float = 0.08,
                 seed: int = 0, tier_skill: dict[str, float] | None = None, **_):
        import random
        self.defect_rate = defect_rate
        self.stubborn_rate = stubborn_rate
        self.random = random.Random(seed)
        self.tier_skill = tier_skill or {}
        self._stubborn: dict[str, bool] = {}

    def _is_stubborn(self, work: Work) -> bool:
        key = f"{work.capability}|{work.instance}"
        if key not in self._stubborn:
            self._stubborn[key] = self.random.random() < self.stubborn_rate
        return self._stubborn[key]

    def run(self, works: list[tuple[Work, str]]) -> list[Return]:
        results = []
        for work, _prompt in works:
            skill = self.tier_skill.get(work.tier, 1.0 - self.defect_rate)
            stubborn = self._is_stubborn(work)
            clean = (not stubborn) and self.random.random() < skill
            record = {
                "capability": work.capability,
                "instance": work.instance,
                "claims": [
                    {"statement": f"mock claim for {work.instance}",
                     "evidence": f"mock://{work.capability}/{work.instance}#L1"}
                ],
                "confidence": round(self.random.uniform(0.5, 0.95), 2),
            }
            if not clean:
                record = self._break(record)
            results.append(Return(work, record, json.dumps(record), True, None,
                                  0.01, 0.0, 91))
        return results

    def _break(self, record: dict) -> dict:
        broken = dict(record)
        which = self.random.choice(["evidence", "claims", "confidence"])
        if which == "evidence":
            broken["claims"] = [{"statement": "a claim with no evidence line"}]
        elif which == "claims":
            broken["claims"] = []
        else:
            broken["confidence"] = 1.7
        return broken


def _pool(fn, works: list[tuple[Work, str]], concurrency: int) -> list[Return]:
    if not works:
        return []
    results: list[Return] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fn, work, prompt): work for work, prompt in works}
        for future in concurrent.futures.as_completed(futures):
            work = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:                          # noqa: BLE001
                # Defence in depth. An exception escaping here would propagate
                # out of the pool and throw away every sibling's completed,
                # already-paid-for work along with the run itself.
                results.append(Return(work, None, "", False,
                                      f"{type(exc).__name__}: {exc}",
                                      transport_error=True))
    return results


# GitHub meters Copilot in premium requests, one per model call, flat across
# models. This is the published overage rate — what the marginal request costs
# once the monthly allowance is spent. It is NOT measured on this account, and
# inside the allowance the marginal cost is nearer zero. Recorded as a named
# constant rather than inlined so that correcting it is a one-line diff.
COPILOT_USD_PER_PREMIUM_REQUEST = 0.04

# Copilot's names for the two capabilities the thick lane must not have. The
# claude-cli adapter denies Bash/Edit/Write; these are the same two doors.
COPILOT_DENIED = ["shell", "write"]


class CopilotDispatcher:
    """GitHub Copilot CLI in print mode. The thick lane's non-Claude family.

    It exists so that thick-lane work has a verifier at all. Every `claude-cli`
    rung is family `claude`, so before this surface `counter_family()` returned
    [] in that lane and the gate had nothing admissible to check with.

    Two things about this adapter are deliberate and worth not undoing.

    **The model cannot be pinned, so one property is checked instead.**
    `--model` exists but `auto` is the only value this account's CLI accepts —
    passing the very id the router reports (`gpt-5.6-luna`) is rejected as "not
    available". The tier therefore names no model and there is no id to match
    against. What is checked is the single property this rung's admissibility
    rests on: that the model which answered was **not Claude family**. If
    GitHub adds a Claude model to the auto router — an ordinary product
    change — every thick-lane gate 2 check would silently become same-family
    verification, which is the one thing `counter_family()` exists to prevent,
    and it would report success while doing it.

    The check fails CLOSED. A return whose model cannot be determined is
    refused rather than accepted, because "we could not tell" and "it was fine"
    must not be the same outcome for the guard that holds up this whole rung.

    **Failures are read from the event stream, not grepped out of the blob.**
    Copilot writes its answer, its MCP server instructions and its errors to
    the same stdout. Searching that text for "429" or "401" classifies a
    correct answer *about* rate limits as an outage, and transport failures are
    excluded from the pass rate — so the defect is silent and it corrupts
    exactly the capability this workspace ships with.
    """

    name = "copilot"
    lane = "thick"

    def __init__(self, concurrency: int = 2, timeout: int = 600,
                 cwd: str | None = None, **_):
        self.concurrency = concurrency
        self.timeout = timeout
        self.cwd = cwd

    def _command(self, prompt: str, usage_path: str) -> list[str]:
        # No model parameter: "auto" is the only value this CLI accepts, and
        # which rung answered is established afterwards in `_one`.
        argv = [
            "copilot", "-p", prompt, "--model", "auto",
            "--output-format", "json", "--no-color", "--log-level", "none",
            # Required for non-interactive: there is no approval surface.
            "--allow-all-tools",
            # ...which is why the two dangerous tools are denied explicitly.
            # --deny-tool takes precedence over --allow-all-tools.
            *[arg for tool in COPILOT_DENIED for arg in ("--deny-tool", tool)],
            # A dispatch must not publish. The default exports the session to
            # GitHub web and mobile, which sends repo content off this machine
            # as a side effect of running a gate.
            "--no-remote-export", "--no-remote",
            # Nothing here should ask a question it cannot receive an answer to.
            "--no-ask-user",
            # Copilot auto-loads AGENTS.md from the working directory into its
            # system prompt. Leaving that on makes the measured context weight
            # a property of where you ran it, and this adapter takes an
            # arbitrary `cwd` — so the same rung would carry a different fixed
            # overhead per launch and the number in tiers.py would mean nothing.
            # It does not restrict file access; the agent can still read.
            "--no-custom-instructions",
            "--usage-output-file", usage_path,
        ]
        return argv

    def _one(self, work: Work, prompt: str) -> Return:
        tier: Tier = BY_NAME[work.tier]
        if shutil.which("copilot") is None:
            return Return(work, None, "", False, "the `copilot` CLI is not on PATH",
                          0.0, transport_error=True)

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            usage_path = str(pathlib.Path(tmp) / "usage.json")
            try:
                proc = subprocess.run(
                    self._command(prompt, usage_path),
                    capture_output=True, text=True,
                    timeout=self.timeout, cwd=self.cwd,
                )
            except subprocess.TimeoutExpired:
                return Return(work, None, "", False,
                              f"copilot timed out after {self.timeout}s",
                              time.monotonic() - started, transport_error=True)
            except Exception as exc:                          # noqa: BLE001
                # Matches the claude-cli adapter: re-raising here would escape
                # future.result() and discard every sibling's paid-for output.
                return Return(work, None, "", False, f"{type(exc).__name__}: {exc}",
                              time.monotonic() - started, transport_error=True)
            seconds = time.monotonic() - started
            usage = _read_json(usage_path)

        events = _jsonl(proc.stdout)
        result = _event(events, "result")
        if result is None:
            return Return(work, None, proc.stdout, False,
                          f"copilot produced no result event "
                          f"(exit {proc.returncode}): {proc.stderr.strip()[:200]}",
                          seconds, transport_error=True)
        # Read the spend BEFORE any early return. AGENTS.md invariant 6:
        # anything that spends a dispatch appears in the run record. A non-zero
        # exit after the model already ran has spent premium requests, and
        # dropping them here under-reports RunResult.cost_usd — the run record
        # is the meta-review's only input, so a cost it cannot see did not
        # happen as far as every later decision is concerned.
        premium = (result.get("usage") or {}).get("premiumRequests", 0) or 0
        cost = premium * COPILOT_USD_PER_PREMIUM_REQUEST
        tokens_in = _session_input_tokens(usage)

        if result.get("exitCode", 1) != 0:
            return Return(work, None, proc.stdout, False,
                          f"copilot exited {result.get('exitCode')}",
                          seconds, cost, tokens_in, transport_error=True)

        # Which model actually answered. Copilot chose it, not us, and it
        # chooses per task: one prompt here resolved to `gpt-5.6-luna` and the
        # next to `mai-code-1.1-flash`. The tier does not name a model for that
        # reason, so there is no id to match — but there is still one property
        # that has to hold.
        # Prefer the answering message: `session.auto_mode_resolved` is
        # @experimental and documents itself as the model settled on for the
        # FIRST prompt of an auto-mode session, which is not the same claim as
        # "the model that produced this answer". It is a fallback, not the
        # source of truth.
        resolved = _event(events, "session.auto_mode_resolved") or {}
        answered = _answer_model(events) or resolved.get("chosenModel")
        if not answered or _is_claude_family(answered):
            # Fails closed on BOTH branches, and they are one branch on
            # purpose. This rung is admissible as a thick-lane verifier for
            # exactly one reason: it is not the family it is checking. A return
            # whose model came back Claude breaks that outright; a return whose
            # model cannot be read leaves it unestablished. Accepting the
            # second while refusing the first would mean the guard protecting
            # the entire gate no-ops the moment an event is missing.
            why = (f"copilot routed to {answered!r}, which is Claude family"
                   if answered else
                   "copilot did not report which model answered")
            return Return(work, None, proc.stdout, False,
                          f"{why}. This rung is admissible as a cross-family "
                          f"verifier only while neither is true, so the return "
                          f"is refused rather than counted as a check.",
                          seconds, cost, tokens_in, inadmissible=True)

        text = _answer_text(events)
        record = extract_json(text)
        if record is None:
            return Return(work, None, text, False, "no json object in the reply",
                          seconds, cost, tokens_in)
        return Return(work, record, text, True, None, seconds, cost, tokens_in)

    def run(self, works: list[tuple[Work, str]]) -> list[Return]:
        if not works:
            return []
        return _pool(self._one, works, self.concurrency)


DISPATCHERS = {
    "kirocc": KiroccDispatcher,
    "claude-cli": ClaudeCliDispatcher,
    "copilot": CopilotDispatcher,
    "mock": MockDispatcher,
}


def get(name: str, **kwargs):
    if name not in DISPATCHERS:
        raise ValueError(f"unknown dispatcher {name!r}. Available: {', '.join(DISPATCHERS)}")
    return DISPATCHERS[name](**kwargs)


def for_tier(tier_name: str, **kwargs):
    """The dispatcher a rung needs, chosen by its surface."""
    return get(BY_NAME[tier_name].surface, **kwargs)
