# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on the repository page, open **Security → Report a
vulnerability**. Don't open a public issue for anything exploitable.

Include what you found, how to reproduce it (the swsdb version from `GET /`, and the request or steps), and its impact
as you understand it. You'll get an acknowledgement within a week, and a fix or a plan once the issue is confirmed.

## Supported versions

Security fixes go into the latest release. Before 1.0, older minor versions don't get backports. Upgrade with a
snapshot first (`swsdb snapshot create`); see [upgrades](docs/guides/backups.md).

## How swsdb is secured

The model (keys, tenants, the SQL sandbox, document permissions, TLS) and what the v0.1 security review found and
fixed are in [docs/guides/security.md](docs/guides/security.md).
