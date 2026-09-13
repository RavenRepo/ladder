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
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from .substrate import KIROCC_BASE
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


DISPATCHERS = {
    "kirocc": KiroccDispatcher,
    "claude-cli": ClaudeCliDispatcher,
    "mock": MockDispatcher,
}


def get(name: str, **kwargs):
    if name not in DISPATCHERS:
        raise ValueError(f"unknown dispatcher {name!r}. Available: {', '.join(DISPATCHERS)}")
    return DISPATCHERS[name](**kwargs)


def for_tier(tier_name: str, **kwargs):
    """The dispatcher a rung needs, chosen by its surface."""
    return get(BY_NAME[tier_name].surface, **kwargs)
