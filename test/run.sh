#!/usr/bin/env bash
# Tests of the action's checks without Cloudflare, Wrangler or a network: the unit self-test, then each helper
# against the fixtures (Wrangler configurations good and bad, tokens/verify responses, a bundle over the budget,
# runtime secrets, `deployments status` and the Wrangler output file). Needs python3 (3.11+ for tomllib).
set -euo pipefail
cd "$(dirname "$0")/.."
cf() { python3 scripts/cloudflare.py --summary "$out/summary.md" "$@"; }
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
fails() { if "$@" > "$out/log" 2>&1; then echo "FAIL: expected failure: $*"; cat "$out/log"; exit 1; fi; }
says() { grep -q -- "$1" "$out/log" || { echo "FAIL: output lacks '$1'"; cat "$out/log"; exit 1; }; }
# `! grep` never trips set -e, so absence gets its own check.
lacks() { if grep -q -- "$1" "$out/log"; then echo "FAIL: output has '$1'"; exit 1; fi; }

python3 scripts/cloudflare.py --self-test

# Lint: the good configurations pass and give the URL of the custom domain.
: > "$out/gh"
cf lint --config test/config/good-worker.jsonc --mode worker --output "$out/gh"
grep -qx 'url=https://www.example.com' "$out/gh" && grep -qx 'worker-name=site' "$out/gh"
cf lint --config test/config/good-static.jsonc --mode static
# The preview environment is another Worker, with Version URLs on and no custom domain required.
: > "$out/gh"
cf lint --config test/config/good-worker.jsonc --mode worker --env preview --preview --output "$out/gh"
grep -qx 'worker-name=site-preview' "$out/gh" && grep -qx 'url=' "$out/gh"
# ...but the production environment cannot serve a preview.
fails cf lint --config test/config/good-worker.jsonc --mode worker --preview
says 'set "preview_urls": true'
# Every rule of the bad configuration shows up, each saying what to change.
fails cf lint --config test/config/bad-worker.jsonc --mode worker
says 'set "workers_dev": false'
says 'set "preview_urls": false'
says '"custom_domain": true'
says 'add "nodejs_compat"'
test "$(grep -c '^::error::' "$out/log")" = 4
# allow-workers-dev and require-custom-domain relax only their own rule.
fails cf lint --config test/config/bad-worker.jsonc --mode worker --allow-workers-dev true --require-custom-domain false
test "$(grep -c '^::error::' "$out/log")" = 2
# TOML, with an environment: the routes come from env.prd, preview_urls is missing.
fails cf lint --config test/config/wrangler.toml --mode worker --env prd
test "$(grep -c '^::error::' "$out/log")" = 1 && says 'preview_urls'
fails cf lint --config test/config/wrangler.toml --mode worker --env staging
says 'no env.staging'
# A static configuration is not a Worker, and a folder without configuration says what artifact-dir must hold.
fails cf lint --config test/config/good-static.jsonc --mode worker
says '"main"'
fails cf lint --dir test --mode static
says 'artifact-dir must hold'

# Token: valid passes quietly, close to expiry warns, invalid and expired fail; the account endpoint may fail
# (user token) while the user endpoint answers.
cf token --response test/token/valid.json --now 2026-10-29T00:00:00+00:00 > "$out/log"
lacks '::warning::'
cf token --response test/token/invalid.json --response test/token/expiring.json --now 2026-10-29T00:00:00+00:00 > "$out/log"
says '::warning::the Cloudflare API token expires in 7 day'
fails cf token --response test/token/invalid.json --response /nonexistent.json
says 'invalid, revoked or expired'
fails cf token --response test/token/expired.json --now 2026-10-29T00:00:00+00:00
fails cf token --response test/token/expiring.json --now 2026-11-06T00:00:00+00:00

# Size: a small bundle passes; 11 MiB of incompressible bytes is over the default 10 MiB budget; source maps do not count.
mkdir -p "$out/small" "$out/big"
printf 'export default { fetch() { return new Response("ok") } }\n' > "$out/small/worker.js"
python3 -c "import os,sys; open(sys.argv[1],'wb').write(os.urandom(64 * 1024 * 1024))" "$out/small/worker.js.map"
cf size --dir "$out/small" > "$out/log"
python3 -c "import os,sys; open(sys.argv[1],'wb').write(os.urandom(11 * 1024 * 1024))" "$out/big/worker.js"
fails cf size --dir "$out/big"
says 'above max-gzip-mib (10 MiB)'
cf size --dir "$out/big" --max-gzip-mib 12 > "$out/log"

# Secrets: every declared name must have a value, values are masked line by line and never printed by the checks.
export SECRETS_JSON='{"SECURITY__HMAC_KEY":"s3cr3t-value-0123456789abcdef","OTHER":"line1\nline2","UNUSED":"x"}'
cf secrets --names 'SECURITY__HMAC_KEY, OTHER' --mask > "$out/log"
says '::add-mask::s3cr3t-value-0123456789abcdef'
says '::add-mask::line2'
test "$(cf secrets --names 'SECURITY__HMAC_KEY')" = '{"SECURITY__HMAC_KEY": "s3cr3t-value-0123456789abcdef"}'
fails cf secrets --names 'SECURITY__HMAC_KEY,MISSING'
says 'missing or empty: MISSING'
lacks 's3cr3t'
SECRETS_JSON='not json s3cr3t' fails cf secrets --names A
lacks 's3cr3t'
fails cf secrets --names 'bad-name'

# Previous version and Wrangler's output file.
test "$(cf previous --status test/wrangler/status-single.json)" = 11111111-1111-4111-8111-111111111111
test "$(cf previous --status test/wrangler/status-split.json | tail -1)" = 11111111-1111-4111-8111-111111111111
printf 'The Worker site has no deployments.\n' > "$out/none.json"
test -z "$(cf previous --status "$out/none.json")"
cf result --file test/wrangler/output.ndjson > "$out/log"
says 'version-id=33333333-3333-4333-8333-333333333333'
says 'preview-url=https://pr-42-site-preview.acme.workers.dev'
cf result --file test/wrangler/deploy.ndjson > "$out/log"
says 'targets=docs.example.com'
echo "fixture tests ok"

# The composite steps of action.yml with fake curl and Wrangler (test/stub): which commands each mode issues.
python3 test/action.py
