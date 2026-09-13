"""The substrate: which dispatch surfaces are actually reachable, right now.

A model catalog is a claim, not a capability. On the machine this was written
for, `pi` advertised 41 models, `omp` 58, and `opencode` 7 — and on probe, all
but two surfaces answered with 401, 429, or nothing at all. A router that
trusts a catalog dispatches into a wall and files the timeouts as research
failures.

So availability is measured, never assumed, and the measurement is a first
class artifact: `probe()` writes a health record that selection reads.

Two axes, and the second one is the one people miss
---------------------------------------------------
A surface has a MODEL TIER (how strong) and a CONTEXT WEIGHT (how much fixed
prompt it drags along before your task text). Measured on this machine with an
identical one-word prompt:

    kirocc HTTP   · haiku-4.5 ·     91 input tokens · subscription-backed
    claude -p     · haiku-4.5 · 22,810 input tokens · $0.0468
    claude -p     · opus-4.5  · 22,804 input tokens · $0.2297

`claude -p` carries ~22.8k tokens of harness — CLAUDE.md, hooks, plugin tool
schemas — and it is the *same* on every model. Dropping opus to haiku inside
that harness saves 4.9x; leaving the harness saves 250x. The dominant term is
context weight, not model price, and a ladder that only orders models is
optimising the smaller one.

Hence `Surface.context_weight`, and hence the rule in SCHEMA.md that a
capability is assigned a LANE before it is assigned a tier.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import shutil
import subprocess
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
HEALTH = ROOT / "20-graph" / "substrate.json"

KIROCC_BASE = "http://127.0.0.1:3456"

# Measured, not guessed. See the module docstring for the experiment.
# Update with `ladder probe --measure-weight`, which re-runs it.
MEASURED_CONTEXT_WEIGHT = {
    "kirocc": 91,
    "claude-cli": 22_800,
}


class Lane:
    """How much fixed context a surface drags along. The primary axis."""

    THIN = "thin"    # ~100 tokens of overhead. Model in, tokens out, no tools.
    THICK = "thick"  # ~23k tokens of overhead. Tools, repo context, filesystem.


@dataclasses.dataclass
class Surface:
    """One reachable way to run a model.

    `name` is what a launch file refers to. `lane` decides whether cheap tiers
    can pay for themselves here at all.
    """

    name: str
    lane: str
    kind: str                      # http | cli
    models: list[str]
    context_weight: int            # input tokens before your prompt
    available: bool = False
    latency_ms: int | None = None
    detail: str = ""
    checked: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# --- probes ---------------------------------------------------------------
# Each probe answers one question: can I get a token out of this thing today?
# Not "is it configured", not "does the catalog list it". One real round trip.

def _http_json(url: str, payload: dict | None, headers: dict, timeout: int):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def probe_kirocc(timeout: int = 30) -> Surface:
    """The local Anthropic-compatible proxy. The thin lane, and the whole ladder.

    Worth knowing: it serves two model FAMILIES — Claude and GPT-5.6 — from one
    localhost endpoint with no API key. That is what makes cross-family
    verification cheap here, and cross-family verification is not a nicety:
    an evaluator's harmful self-preference shows up precisely when it errs as a
    generator (arXiv:2504.03846), which is exactly the case a gate exists to
    catch. See docs/principles.md.
    """
    surface = Surface(
        name="kirocc",
        lane=Lane.THIN,
        kind="http",
        models=[],
        context_weight=MEASURED_CONTEXT_WEIGHT["kirocc"],
        checked=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    try:
        catalog = _http_json(f"{KIROCC_BASE}/v1/models", None,
                             {"content-type": "application/json"}, timeout)
        surface.models = [m["id"] for m in catalog.get("data", [])]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        surface.detail = f"catalog unreachable: {type(exc).__name__}: {exc}"
        return surface

    if not surface.models:
        surface.detail = "catalog is empty"
        return surface

    # A catalog entry is still only a claim. Spend one token proving it.
    cheapest = _cheapest_known(surface.models)
    started = time.monotonic()
    try:
        reply = _http_json(
            f"{KIROCC_BASE}/v1/messages",
            {"model": cheapest, "max_tokens": 8,
             "messages": [{"role": "user", "content": "Say OK"}]},
            {"content-type": "application/json",
             # /v1/messages rejects requests without this header.
             "X-Claude-Code-Session-Id": "ladder-probe"},
            timeout,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        surface.detail = f"{cheapest} did not answer: {type(exc).__name__}: {exc}"
        return surface

    surface.latency_ms = int((time.monotonic() - started) * 1000)
    text = "".join(block.get("text", "") for block in reply.get("content", []))
    if not text.strip():
        surface.detail = f"{cheapest} answered empty"
        return surface

    surface.available = True
    surface.detail = f"{len(surface.models)} models, probed {cheapest}"
    return surface


def probe_claude_cli(timeout: int = 180) -> Surface:
    """Claude Code in print mode. The thick lane: tools, repo, filesystem.

    Expensive to probe, because probing it pays the 22.8k harness tax like any
    other call. That is the point of recording the tax rather than rediscovering
    it: you should not route a one-line classification here, and the number is
    the argument.
    """
    surface = Surface(
        name="claude-cli",
        lane=Lane.THICK,
        kind="cli",
        models=["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5-20251001"],
        context_weight=MEASURED_CONTEXT_WEIGHT["claude-cli"],
        checked=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    if shutil.which("claude") is None:
        surface.detail = "the `claude` CLI is not on PATH"
        return surface

    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "-p", "Say OK", "--output-format", "json",
             "--model", "claude-haiku-4-5-20251001",
             "--permission-prompts", "none",
             "--disallowed-tools", "Bash", "Edit", "Write"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        surface.detail = f"no answer within {timeout}s"
        return surface
    except OSError as exc:
        surface.detail = f"{type(exc).__name__}: {exc}"
        return surface

    surface.latency_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        surface.detail = f"exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        return surface
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        surface.detail = "did not return json"
        return surface

    surface.available = True
    cost = envelope.get("total_cost_usd", 0.0)
    surface.detail = f"probe cost ${cost:.4f} (this is the harness tax, not the answer)"
    return surface


def probe_cli(name: str, argv: list[str], lane: str, models: list[str],
              weight: int, timeout: int = 150) -> Surface:
    """Generic probe for the CLIs that were dead on this machine.

    Kept, rather than deleted, because they are one credential away from being
    the cheap tier this whole workspace wants. The probe is what will notice.
    """
    surface = Surface(name=name, lane=lane, kind="cli", models=models,
                      context_weight=weight,
                      checked=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    if shutil.which(argv[0]) is None:
        surface.detail = f"`{argv[0]}` is not on PATH"
        return surface
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        surface.detail = f"no answer within {timeout}s"
        return surface
    except OSError as exc:
        surface.detail = f"{type(exc).__name__}: {exc}"
        return surface

    surface.latency_ms = int((time.monotonic() - started) * 1000)
    blob = (proc.stdout or "") + (proc.stderr or "")
    # These CLIs exit 0 while reporting the failure inside their own event
    # stream, so the exit code is not the signal. The error string is.
    for marker in ("401", "429", "CreditsError", "Unauthorized",
                   "Insufficient balance", "rate limit"):
        if marker.lower() in blob.lower():
            surface.detail = f"reachable but refused: {marker}"
            return surface
    if not blob.strip():
        surface.detail = "returned nothing"
        return surface
    surface.available = True
    surface.detail = "answered"
    return surface


def _cheapest_known(models: list[str]) -> str:
    """Pick the cheapest model to spend the probe token on.

    Ordered by the tier table in SCHEMA.md, falling back to whatever the
    catalog offers first rather than failing — the probe's job is to get one
    round trip, not to be clever.
    """
    for preferred in ("claude-haiku-4.5", "gpt-5.6-sol", "claude-sonnet-4.5"):
        if preferred in models:
            return preferred
    return models[0]


# --- the registry ---------------------------------------------------------

def probe_all(include_dead: bool = True, timeout: int = 30) -> list[Surface]:
    """Probe every surface this workspace knows how to dispatch to.

    The two surfaces that work are always probed. `include_dead` adds the ones
    that were refusing on this machine — skip them for a fast check, but run
    the full probe weekly, because "dead" is a credential state, not a
    property, and the probe is the only thing that will notice when a card
    gets paid.
    """
    surfaces = [probe_kirocc(timeout=timeout), probe_claude_cli()]
    if include_dead:
        surfaces += [
            probe_cli("opencode", ["opencode", "run", "-m",
                                   "opencode/nemotron-3.5-lightning-free",
                                   "--format", "json", "Say OK"],
                      Lane.THICK, ["opencode/*-free"], 0),
            probe_cli("pi", ["pi", "-p", "--provider", "opencode-go",
                             "--model", "qwen3.8-flash", "--mode", "json",
                             "--no-tools", "--no-session", "Say OK"],
                      Lane.THIN, ["opencode-go/*"], 0),
            probe_cli("omp", ["omp", "-p", "--model",
                              "google-antigravity/gemini-3.5-flash-lite",
                              "--mode", "json", "--no-tools", "--no-session",
                              "Say OK"],
                      Lane.THIN, ["google-antigravity/*"], 0),
            probe_cli("codex", ["codex", "exec", "--skip-git-repo-check", "Say OK"],
                      Lane.THICK, ["gpt-5.x"], 0),
        ]
    return surfaces


def write_health(surfaces: list[Surface], path: pathlib.Path = HEALTH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "surfaces": [s.to_dict() for s in surfaces],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def read_health(path: pathlib.Path = HEALTH) -> dict:
    if not path.exists():
        return {"checked": None, "surfaces": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # A corrupt health file must not read as "everything is fine". An empty
        # surface list makes selection refuse to dispatch, which is the safe
        # direction.
        return {"checked": None, "surfaces": [], "corrupt": True}


def available(path: pathlib.Path = HEALTH) -> list[str]:
    return [s["name"] for s in read_health(path).get("surfaces", []) if s.get("available")]
