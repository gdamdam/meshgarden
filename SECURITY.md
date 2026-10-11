# Security Policy

Meshgarden is a one-maintainer, experimental project. This policy sets expectations, not
guarantees.

## Supported versions

| Version | Supported                               |
| ------- | --------------------------------------- |
| 1.10.x  | Yes (current)                           |
| < 1.10  | No, upgrade to the latest release       |

## Reporting a vulnerability

**Do not open a public issue for security problems.** Use GitHub's private vulnerability reporting:
open the [Security tab](https://github.com/gdamdam/meshgarden/security) and click "Report a
vulnerability", or go straight to
<https://github.com/gdamdam/meshgarden/security/advisories/new>.

Please include:

- the Meshgarden version (`mgarden --version`)
- the firmware version of the affected radio
- a sanitized reproduction, with no channel keys, PINs or personal identifiers

There is no SLA, but reports are acknowledged and triaged as soon as reasonably possible.

## Scope

Any way to break Meshgarden's core safety guarantees is in scope:

- **Replies:** a channel broadcast must never trigger a reply (the only exception is the documented
  public command subset in MeshCore `channel` mode).
- **Confidentiality:** everything sent over the mesh is visible to anyone holding the channel key.
  There is no extra confidentiality layer.
- **Local endpoints:** the dashboard and metrics endpoint are loopback-only by default. Exposing
  them beyond localhost without explicit configuration is a bug.
