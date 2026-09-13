"""A launch: one question, one capability, and the rules for this sweep.

Markdown with a YAML-ish front block, because a launch is a thing a human reads
and edits, and because the front block is the part a machine has to agree with.

The launch is deliberately NOT a list of work with a model attached. It names a
capability and supplies instances; which rung runs them is the policy's answer,
derived from the outcome history. That is the difference between a script and a
workflow: next month this same file dispatches somewhere else without anyone
editing it.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHES = ROOT / "00-launches"

_BLOCK = re.compile(r"^---\s*$\n(.*?)^---\s*$\n", re.MULTILINE | re.DOTALL)

RISK_CLASSES = ("reversible", "costly", "irreversible")


@dataclasses.dataclass
class Launch:
    name: str
    capability: str
    question: str
    lane: str = "thin"
    floor_tier: str = "sonnet-5"
    risk_class: str = "reversible"
    gate: str = "script"
    instances: list[str] = dataclasses.field(default_factory=list)
    prompt_template: str = "{instance}"
    return_schema: str = ""
    max_dispatches: int = 60
    max_seconds: float = 1800.0
    max_attempts: int = 2
    weights: dict = dataclasses.field(default_factory=lambda: {"w_usd": 1.0})
    body: str = ""

    def prompt_for(self, instance: str) -> str:
        return self.prompt_template.replace("{instance}", instance)

    def validate(self) -> list[str]:
        """Errors that would make this launch dispatch into nothing.

        Cheapest check in the workspace. Run it before every real run: a launch
        that names a tier outside its own lane will select nothing and report a
        clean, empty, expensive success.
        """
        from .tiers import BY_NAME

        errors = []
        if self.lane not in ("thin", "thick"):
            errors.append(f"lane must be thin or thick, got {self.lane!r}")
        if self.risk_class not in RISK_CLASSES:
            errors.append(f"risk_class must be one of {RISK_CLASSES}, got {self.risk_class!r}")
        if self.gate not in ("script", "checkable", "full"):
            errors.append(f"gate must be script, checkable or full, got {self.gate!r}")
        if self.floor_tier not in BY_NAME:
            errors.append(f"floor_tier {self.floor_tier!r} is not a rung on the ladder")
        elif BY_NAME[self.floor_tier].lane != self.lane:
            errors.append(
                f"floor_tier {self.floor_tier!r} is in the "
                f"{BY_NAME[self.floor_tier].lane} lane but this launch declares "
                f"{self.lane} — selection would find nothing")
        if not self.instances:
            errors.append("no instances: this launch would dispatch nothing")
        if "{instance}" not in self.prompt_template:
            errors.append("prompt_template never interpolates {instance}: "
                          "every agent would get the same task")
        if self.risk_class == "irreversible" and self.gate != "full":
            # Anything that sends, pays or publishes does not get the cheap
            # half of the gate and nothing else.
            errors.append("irreversible work requires gate: full")
        return errors


def parse(text: str, name: str) -> Launch:
    match = _BLOCK.search(text)
    fields: dict[str, str] = {}
    body = text
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            if ":" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    instances = [line.strip("- ").strip()
                 for line in _section(body, "instances").splitlines()
                 if line.strip().startswith("-")]
    launch = Launch(
        name=name,
        capability=fields.get("capability", name),
        question=fields.get("question", ""),
        lane=fields.get("lane", "thin"),
        floor_tier=fields.get("floor_tier", "sonnet-5"),
        risk_class=fields.get("risk_class", "reversible"),
        gate=fields.get("gate", "script"),
        instances=instances,
        prompt_template=_section(body, "prompt").strip() or "{instance}",
        return_schema=_section(body, "return").strip(),
        max_dispatches=int(fields.get("max_dispatches", 60)),
        max_seconds=float(fields.get("max_seconds", 1800)),
        max_attempts=int(fields.get("max_attempts", 2)),
        weights=_weights(fields.get("optimise", "usd")),
        body=body,
    )
    return launch


def _weights(spec: str) -> dict:
    """`optimise: seconds` and friends.

    kirocc meters no dollars, so a kirocc launch that optimises usd scores every
    rung at zero and the ladder stops meaning anything. Naming the objective in
    the launch is how that stays visible.
    """
    spec = spec.strip().lower()
    return {
        "usd": {"w_usd": 1.0},
        "seconds": {"w_usd": 0.0, "w_seconds": 1.0},
        "tokens": {"w_usd": 0.0, "w_tokens": 1.0},
        "balanced": {"w_usd": 1.0, "w_tokens": 0.01, "w_seconds": 0.05},
    }.get(spec, {"w_usd": 1.0})


def _section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^##\s+{heading}\s*$\n(.*?)(?=^##\s|\Z)",
                         re.MULTILINE | re.DOTALL | re.IGNORECASE)
    match = pattern.search(text)
    return match.group(1) if match else ""


def load_launch(name: str, directory: pathlib.Path | None = None) -> Launch:
    directory = directory or LAUNCHES
    path = directory / (name if name.endswith(".md") else f"{name}.md")
    if not path.exists():
        raise FileNotFoundError(f"no launch at {path}")
    launch = parse(path.read_text(encoding="utf-8"), path.stem)
    errors = launch.validate()
    if errors:
        raise ValueError(f"{path.name} is not runnable:\n  - " + "\n  - ".join(errors))
    return launch


def list_launches(directory: pathlib.Path | None = None) -> list[Launch]:
    directory = directory or LAUNCHES
    out = []
    for path in sorted(directory.glob("*.md")):
        if path.stem.upper() == "README":
            continue
        try:
            out.append(parse(path.read_text(encoding="utf-8"), path.stem))
        except (OSError, ValueError):
            continue
    return out
