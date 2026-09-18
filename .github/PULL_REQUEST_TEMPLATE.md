## What changed and why

<!-- Describe the change. Name the defect it prevents or the capability it adds. -->

## Checklist

- [ ] `python3 -m unittest discover -s tests -t .` — all tests pass
- [ ] `./ladder lint` — all launches pass (if you touched a launch or the gate)
- [ ] `./ladder run <launch> --dispatch mock` — pipeline completes (if you touched dispatch, gate, or policy)
- [ ] New rules/thresholds have a test named after the defect they prevent
- [ ] Changes to `gate.py` or `policy.py` reference which section of `docs/principles.md` was reviewed
