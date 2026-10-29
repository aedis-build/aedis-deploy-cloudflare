# Security policy

## Supported versions

One major at a time: the current one (`v1`). The previous major receives fixes until the first minor of the new one.
Callers pin the commit SHA of a release tag (`@<sha> # vX.Y.Z`), never a branch or a moving tag.

## Reporting a vulnerability

Use **Report a vulnerability** in this repository's Security tab (GitHub Private Vulnerability Reporting). Do not open a public issue for vulnerabilities.

- Acknowledgement within 3 business days.
- Initial severity assessment (CVSS) within 7 days.
- Fix or documented mitigation for high/critical severity within 30 days.
- Credit to the reporter in the advisory, if desired.

## Scope

`action.yml` and `scripts/`: what runs in the caller's job, how it handles the Cloudflare API token and the runtime
secrets it is given, and the checks it applies before publishing. Wrangler itself is covered by
[cloudflare/workers-sdk](https://github.com/cloudflare/workers-sdk/security/policy) and comes from the caller's
lockfile; the Aedis Platform reusable workflows this repository calls by
[aedis-build/aedis-platform](https://github.com/aedis-build/aedis-platform/security/policy).

## Practices of this repository

- The API token reaches `curl` through a config on stdin and Wrangler through its environment, never as an argument;
  runtime secrets reach Wrangler on stdin, each value masked first, never written to disk or echoed.
- Every action pinned by commit SHA; `permissions: {}` by default; no Wrangler installed globally.
- Dependencies updated by Dependabot; OpenSSF Scorecard published; releases immutable and attested by GitHub.
