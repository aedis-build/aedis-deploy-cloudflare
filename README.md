# Aedis Deploy Cloudflare

GitHub Action that **publishes a site your job already built and verified to Cloudflare Workers**, and refuses to
when the deploy would be unsafe. Two modes:

- `static`: a Worker with static assets only (Next.js `output: "export"`, any static site), published with
  `wrangler deploy`.
- `worker`: a Worker with server code (Next.js through [OpenNext](https://opennext.js.org/cloudflare), or any
  Worker), published as a version: `wrangler versions upload`, then `wrangler versions deploy` with an optional
  traffic percentage and `wrangler triggers deploy`. With `preview-alias` it only uploads the version, for a pull
  request preview. With `rollback-to` it only moves all traffic back to an earlier version.

Before anything is uploaded it verifies the API token (fails when it is invalid, warns when it expires in under 14
days), lints the Wrangler configuration of the target environment (`workers_dev`, `preview_urls`, a custom domain,
`nodejs_compat`) with messages that say what to change, and, in worker mode, measures the bundle against a budget.
Runtime secrets reach Wrangler on stdin, attached to the version they belong to.

Wrangler comes from **your lockfile** (`pnpm exec wrangler`), never from a global install, so its version is
reviewed and updated like any other dependency. Building, attesting and verifying the artifact belong to your
workflow (or to the [Aedis Platform](https://github.com/aedis-build/aedis-platform) `deploy-site.yml` reusable,
which does all three): this action publishes the bytes it finds in `artifact-dir`. Part of the Aedis Platform,
usable on its own.

## Usage

### Static site

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: { name: prd, url: https://docs.example.com }
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: pnpm/action-setup@ea17c68df8912ef543352723c149a84f56e3d413 # v6.1.0
      - uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0
        with:
          node-version: 22
          cache: pnpm
      - run: pnpm install --frozen-lockfile   # only for Wrangler
      # ...download the built site into ./out and verify its attestation...
      - uses: aedis-build/aedis-deploy-cloudflare@<sha of the release tag> # vX.Y.Z
        with:
          mode: static
          account-id: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
          api-token: ${{ secrets.CLOUDFLARE_API_TOKEN }}
```

`wrangler.jsonc` at the repository root:

```jsonc
{
  "name": "docs",
  "compatibility_date": "2026-10-01",
  "assets": { "directory": "./out", "not_found_handling": "404-page" },
  "workers_dev": false,
  "preview_urls": false,
  "routes": [{ "pattern": "docs.example.com", "custom_domain": true }]
}
```

### Worker (Next.js with OpenNext), gradual rollout and runtime secrets

```yaml
      # ...after downloading and verifying the attested .open-next/ and wrangler.jsonc into apps/web...
      - uses: aedis-build/aedis-deploy-cloudflare@<sha of the release tag> # vX.Y.Z
        id: deploy
        with:
          mode: worker
          artifact-dir: apps/web
          environment-name: staging
          percentage: 25
          secret-names: SECURITY__HMAC_KEY
          secrets-json: ${{ toJSON(secrets) }}
          account-id: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
          api-token: ${{ secrets.CLOUDFLARE_API_TOKEN }}
      # ...smoke test ${{ steps.deploy.outputs.url }}; if it fails, send the traffic back:
      - if: failure() && steps.deploy.outputs.previous-version-id != ''
        uses: aedis-build/aedis-deploy-cloudflare@<sha of the release tag> # vX.Y.Z
        with:
          mode: worker
          artifact-dir: apps/web
          environment-name: staging
          rollback-to: ${{ steps.deploy.outputs.previous-version-id }}
          account-id: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
          api-token: ${{ secrets.CLOUDFLARE_API_TOKEN }}
```

`apps/web/wrangler.jsonc`:

```jsonc
{
  "name": "site",
  "main": ".open-next/worker.js",
  "compatibility_date": "2026-10-01",
  "compatibility_flags": ["nodejs_compat", "global_fetch_strictly_public"],
  "assets": { "directory": ".open-next/assets", "binding": "ASSETS" },
  "workers_dev": false,
  "preview_urls": false,
  "env": {
    "staging": { "routes": [{ "pattern": "staging.example.com", "custom_domain": true }] },
    "prd": { "routes": [{ "pattern": "www.example.com", "custom_domain": true }] },
    // Pull request previews: a separate Worker (site-preview) with Version URLs on, no route, its own bindings.
    "preview": { "preview_urls": true }
  }
}
```

Only the names in `secret-names` are read from `secrets-json`; passing `toJSON(secrets)` is convenient, a JSON
object with just those names is tighter. A declared secret that is missing or empty fails the job and names it.

Pin the action by the commit SHA of a release tag with the tag in the comment, as above: Dependabot
(`package-ecosystem: github-actions`) then opens the repin pull request at every release, SHA and comment together.
Runs on Linux runners with Python 3.11+ and pnpm (`ubuntu-latest` has Python; add `pnpm/action-setup`).

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `mode` | required | `static` (assets only, `wrangler deploy`) or `worker` (versions upload/deploy) |
| `artifact-dir` | `.` | Folder with `wrangler.json`, `wrangler.jsonc` or `wrangler.toml` and the built output it points to; Wrangler runs here |
| `environment-name` | `""` | Wrangler environment (`env.<name>`); empty uses the top level |
| `account-id` | required | Cloudflare account id |
| `api-token` | required | Cloudflare API token, scopes below; never logged |
| `secret-names` | `""` | Runtime secrets of the Worker, comma- or newline-separated (worker mode) |
| `secrets-json` | `{}` | JSON object of name to value; only `secret-names` are read; values masked, never echoed |
| `percentage` | `100` | Share of traffic for the new version; below 100 the previous version keeps the rest (worker mode) |
| `preview-alias` | `""` | Upload a version with this alias (e.g. `pr-42`) and deploy nothing (worker mode) |
| `rollback-to` | `""` | Version id that gets 100 % of the traffic; nothing is uploaded or linted. Check that version's provenance before calling |
| `require-custom-domain` | `true` | Fail unless a route has `custom_domain: true` (not checked for previews) |
| `allow-workers-dev` | `false` | Accept `workers_dev: true` |
| `max-gzip-mib` | `10` | Budget of the gzipped Worker bundle, in MiB (worker mode) |

## Outputs

| Output | Meaning |
|---|---|
| `version-id` | Version now serving (or uploaded, for a preview; or the rollback target) |
| `previous-version-id` | Version that served the traffic before (empty on the first deploy): the rollback target |
| `url` | `https://<first custom domain>` of the target environment, or the first target Wrangler reports; empty for previews |
| `preview-url` | `https://<alias>-<worker>.<subdomain>.workers.dev` of a preview |

## What it checks

**Token.** `GET /accounts/<id>/tokens/verify` (account-owned tokens) and `GET /user/tokens/verify` (user tokens);
the token goes to `curl` in a config on stdin, never on the command line. Invalid, revoked or expired fails the job;
expiry in under 14 days is a warning in the log and the step summary; a token with no expiry is noted. Cloudflare
does not accept GitHub OIDC, so the token is a long-lived secret: give it an expiry and rotate it.

**Wrangler configuration** of the target environment (top level merged with `env.<name>`), before any upload:

| Rule | Why |
|---|---|
| `workers_dev: false` | The site answers on its own domain, never on `<name>.<account>.workers.dev`, where your WAF and headers rules do not run |
| `preview_urls: false`, except for a preview, which needs `true` | Every uploaded version otherwise gets a public URL; keep Version URLs on only in the preview Worker |
| a route with `custom_domain: true` | Cloudflare owns the DNS record and certificate of the hostname, and the Worker is the origin |
| `nodejs_compat` in `compatibility_flags` (worker mode) | Next.js on Workers needs the Node.js APIs |
| `main` (worker) or `assets.directory` (static), `name`, `compatibility_date` | What the mode publishes |

**Bundle size** (worker mode). `wrangler deploy --dry-run --outdir` bundles without credentials; the action sums the
gzipped size of the bundle (source maps excluded) and fails above `max-gzip-mib`, and above the 64 MiB uncompressed
that Cloudflare accepts. Cloudflare dropped its 10 MiB compressed limit in September 2026, so the default budget is
now a guard against a bloated server bundle (cold starts), not a platform limit; raise it on purpose.

**Runtime secrets** (worker mode). Each value is masked (`::add-mask::`, line by line) before anything else runs,
then the JSON of the declared names is piped to `wrangler versions upload --secrets-file /dev/stdin` (or
`wrangler deploy` on the first deploy). Attached to the version, the secrets follow it: a rollback brings back the
secrets that version ran with, and a split deployment keeps each version's own. `wrangler secret bulk` would instead
create a separate deployment of the code already running, which a gradual rollout cannot share.

## Deploy flow (worker mode)

1. `wrangler deployments status --json` finds the version serving now (`previous-version-id`).
2. No deployment yet: `wrangler deploy` creates the Worker, its routes and Durable Object classes (a first deploy
   ignores `percentage`).
3. Otherwise `wrangler versions upload`, then `wrangler versions deploy <new>@P% <previous>@(100-P)% -y` and
   `wrangler triggers deploy`, which applies routes, custom domains and the `workers.dev`/Version URL settings
   (`versions deploy` does not).
4. With `preview-alias`, only step 3's upload, with `--preview-alias`.

Durable Object class changes (new, renamed or deleted classes) cannot go out as a version: Cloudflare applies them
atomically. Ship such a change alone, and expect `versions upload` to fail with an explicit error until it is.

Previews use aliased Version URLs, which run with the bindings of the Worker they belong to. That is why the preview
environment must be a separate Worker (`env.preview`) with its own bindings and secrets, never the production
Worker with `preview_urls: true`. Cloudflare's newer [Previews](https://developers.cloudflare.com/workers/previews/)
isolate resources per branch; this action does not use them yet.

## Cloudflare API token

Create a custom token (**My Profile → API Tokens**, or an account-owned token under **Manage Account → API
Tokens**) with only:

| Permission | Scope | Needed for |
|---|---|---|
| Account · Workers Scripts · Edit | the account | uploading versions, deploying, secrets, Durable Object classes and the `send_email` binding |
| Zone · Workers Routes · Edit | only the zone(s) of the site | custom domains and routes (`wrangler triggers deploy`, first `wrangler deploy`) |

- **Custom domain**: Workers Routes · Edit on the zone is enough; Cloudflare creates the DNS record and the
  certificate itself. No DNS or SSL permission is needed.
- **Durable Objects**: covered by Workers Scripts · Edit; deploying a binding needs no permission on the bound
  resource.
- **Email Routing** (`send_email`): covered by Workers Scripts · Edit. Enabling Email Routing on the zone and
  verifying the destination address is a one-time setup (dashboard or Terraform), not something the deploy token
  should be able to do.
- Set an expiry (e.g. 90 days): the action warns two weeks ahead. Keep one token per GitHub environment (`preview`,
  `staging`, `prd`), each limited to its zone, so a preview can never publish production.

## License

Apache-2.0. See [LICENSE](LICENSE).
