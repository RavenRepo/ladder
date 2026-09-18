# Security Policy

## Scope

ladder is a CLI orchestrator that dispatches prompts to external LLM APIs. It
does **not** store, log, or transmit API keys or credentials itself -- it relies
on the external surfaces (HTTP endpoints, CLI harnesses) that you configure to
handle authentication.

Security concerns most likely to be in scope:

- Command injection via crafted prompt or gate output
- Unexpected subprocess behavior in CLI-harness (thick-lane) surfaces
- File-system path traversal in log or artifact handling

## Reporting a Vulnerability

If you discover a security issue, please report it responsibly:

1. **Preferred:** Open a [private security advisory][advisory] on the GitHub
   repository. This keeps the report confidential until a fix is released.
2. **Alternative:** Email the maintainers directly (see the commit history for
   contact addresses).

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a proof of concept if possible
- The version of ladder and Python you are using

We will acknowledge receipt within 72 hours and aim to release a fix or
mitigation within 14 days of confirmation.

## Supported Versions

Only the latest release on the default branch receives security fixes. There
are no long-term support branches at this time.

## Disclosure

We follow coordinated disclosure. Once a fix is available, we will publish a
description of the issue, credit the reporter (unless they prefer anonymity),
and tag a new release.

[advisory]: https://github.com/RavenRepo/ladder/security/advisories/new
