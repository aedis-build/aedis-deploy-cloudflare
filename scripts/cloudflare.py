#!/usr/bin/env python3
"""Helpers of the Aedis Deploy Cloudflare action, each one testable without a network.

  lint     checks the Wrangler configuration of the target environment before anything is uploaded
  token    reads the responses of GET /accounts/<id>/tokens/verify and /user/tokens/verify
  size     measures the bundle `wrangler deploy --dry-run --outdir` wrote (gzip and raw)
  secrets  validates the declared runtime secrets, masks their values, prints the JSON Wrangler reads on stdin
  previous picks the version serving traffic from `wrangler deployments status --json`
  result   reads the ND-JSON of WRANGLER_OUTPUT_FILE_PATH (version id, preview URLs, targets)

Messages go to stdout as GitHub workflow commands (::error::, ::warning::) and, with --summary, to the step summary.
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import tomllib
from pathlib import Path

CONFIG_NAMES = ("wrangler.json", "wrangler.jsonc", "wrangler.toml")  # Wrangler's own order
RAW_LIMIT_MIB = 64  # uncompressed Worker size Cloudflare enforces on every plan (since 2026-09-04)


def strip_jsonc(text):
    """Removes // and /* */ comments and trailing commas outside strings, so json.loads reads JSONC."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True; out.append(c); i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c); i += 1
    cleaned, res, in_str, i = "".join(out), [], False, 0
    while i < len(cleaned):
        c = cleaned[i]
        if in_str:
            res.append(c)
            if c == "\\" and i + 1 < len(cleaned):
                res.append(cleaned[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True; res.append(c)
        elif c == ",":
            j = i + 1
            while j < len(cleaned) and cleaned[j] in " \t\r\n":
                j += 1
            if j < len(cleaned) and cleaned[j] in "]}":
                i += 1; continue
            res.append(c)
        else:
            res.append(c)
        i += 1
    return "".join(res)


def find_config(directory):
    for name in CONFIG_NAMES:
        p = Path(directory) / name
        if p.is_file():
            return p
    return None


def load_config(path):
    text = path.read_text(encoding="utf-8")
    return tomllib.loads(text) if path.suffix == ".toml" else json.loads(strip_jsonc(text))


def resolve_env(cfg, env):
    """Top-level keys with the environment's own values on top (the keys this action checks are inheritable)."""
    if not env:
        return dict(cfg)
    envs = cfg.get("env") or {}
    if env not in envs:
        raise SystemExit(f"::error::the Wrangler configuration has no env.{env}; add it or fix environment-name")
    merged = {k: v for k, v in cfg.items() if k != "env"}
    merged.update(envs[env])
    merged["name"] = envs[env].get("name") or f"{cfg.get('name', '')}-{env}"
    return merged


def routes_of(c):
    routes = c.get("routes") or ([c["route"]] if c.get("route") else [])
    return [r if isinstance(r, dict) else {"pattern": r} for r in routes]


def lint(c, mode, preview, require_custom_domain, allow_workers_dev):
    """Returns (errors, notes); each error says what to change in the Wrangler configuration."""
    errors, notes = [], []
    if not c.get("name"):
        errors.append('set "name": the Worker this deploy targets')
    if c.get("workers_dev") is not False and not allow_workers_dev:
        errors.append('set "workers_dev": false: the site answers only on its own domain, never on <name>.<account>.workers.dev')
    if preview:
        if c.get("preview_urls") is not True:
            errors.append('a preview needs Version URLs: set "preview_urls": true in the Wrangler environment used for previews '
                          '(a separate Worker, e.g. env.preview), never in the production one')
    elif c.get("preview_urls") is not False:
        errors.append('set "preview_urls": false: every uploaded version of this Worker would otherwise get a public workers.dev URL')
    routes = routes_of(c)
    custom = [r for r in routes if r.get("custom_domain") is True]
    if not preview and require_custom_domain and not custom:
        errors.append('add a route with "custom_domain": true, e.g. "routes": [{ "pattern": "www.example.com", "custom_domain": true }]: '
                      "Cloudflare then owns the DNS record and the certificate of the hostname")
    if mode == "worker":
        if not c.get("main"):
            errors.append('mode worker needs "main" (OpenNext: ".open-next/worker.js")')
        flags = c.get("compatibility_flags") or []
        if not any(str(f).startswith("nodejs_compat") for f in flags):
            errors.append('add "nodejs_compat" to "compatibility_flags": Next.js on Workers (OpenNext) needs the Node.js APIs')
    else:
        if not (c.get("assets") or {}).get("directory"):
            errors.append('mode static needs "assets": { "directory": "<folder of the built site>" }')
        if c.get("main"):
            notes.append('"main" is set: mode static still runs `wrangler deploy`, which uploads that Worker code too')
    if not c.get("compatibility_date"):
        errors.append('set "compatibility_date" (the date of the runtime behaviour this Worker was tested with)')
    url = f"https://{custom[0]['pattern'].rstrip('/*')}" if custom else ""
    return errors, notes, url


def emit(errors, warnings=(), notes=(), summary=None, title=""):
    for e in errors:
        print(f"::error::{e}")
    for w in warnings:
        print(f"::warning::{w}")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            if title:
                f.write(f"### {title}\n\n")
            for e in errors:
                f.write(f"- :x: {e}\n")
            for w in warnings:
                f.write(f"- :warning: {w}\n")
            for n in notes:
                f.write(f"- {n}\n")
            f.write("\n")


def cmd_lint(a):
    path = Path(a.config) if a.config else find_config(a.dir)
    if not path:
        emit([f"no wrangler.json, wrangler.jsonc or wrangler.toml in {a.dir}: artifact-dir must hold the Wrangler configuration"], summary=a.summary)
        return 1
    c = resolve_env(load_config(path), a.env)
    errors, notes, url = lint(c, a.mode, a.preview, a.require_custom_domain == "true", a.allow_workers_dev == "true")
    emit(errors, notes=[f"configuration `{path}`" + (f", environment `{a.env}`" if a.env else "") + f", Worker `{c.get('name')}`"] + notes,
         summary=a.summary, title="Wrangler configuration")
    if a.output:
        with open(a.output, "a", encoding="utf-8") as f:
            f.write(f"config={path}\nworker-name={c.get('name', '')}\nurl={url}\n")
    return 1 if errors else 0


def cmd_token(a):
    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    for resp in a.response:
        try:
            body = json.loads(Path(resp).read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            continue
        result = body.get("result") or {}
        if not body.get("success") or result.get("status") != "active":
            continue
        expires = result.get("expires_on")
        if not expires:
            emit([], notes=["Cloudflare API token valid, with no expiry date: give the next one an expiry (`expires_on`)"],
                 summary=a.summary, title="Cloudflare API token")
            return 0
        left = dt.datetime.fromisoformat(expires.replace("Z", "+00:00")) - now
        if left.total_seconds() <= 0:
            break
        days = left.total_seconds() / 86400
        warn = [f"the Cloudflare API token expires in {days:.0f} day(s) ({expires}): rotate it in the GitHub environment"] if days < a.warn_days else []
        emit([], warn, [f"Cloudflare API token valid until {expires}"], summary=a.summary, title="Cloudflare API token")
        return 0
    emit(["the Cloudflare API token is invalid, revoked or expired (GET /tokens/verify): create a new one with the scopes in the README "
          "and store it as CLOUDFLARE_API_TOKEN in the GitHub environment"], summary=a.summary, title="Cloudflare API token")
    return 1


def cmd_size(a):
    files = [p for p in Path(a.dir).rglob("*") if p.is_file() and p.suffix != ".map"]
    if not files:
        emit([f"the dry run left no bundle in {a.dir}"], summary=a.summary)
        return 1
    raw = sum(p.stat().st_size for p in files)
    gz = sum(len(gzip.compress(p.read_bytes(), 9)) for p in files)
    mib = 1024 * 1024
    errors = []
    if gz > a.max_gzip_mib * mib:
        errors.append(f"the Worker bundle is {gz / mib:.2f} MiB gzipped, above max-gzip-mib ({a.max_gzip_mib:g} MiB): "
                      "drop dependencies from the server bundle or raise the budget on purpose")
    if raw > RAW_LIMIT_MIB * mib:
        errors.append(f"the Worker bundle is {raw / mib:.2f} MiB uncompressed, above the {RAW_LIMIT_MIB} MiB Cloudflare accepts")
    emit(errors, notes=[f"Worker bundle: {raw / mib:.2f} MiB raw, {gz / mib:.2f} MiB gzip (budget {a.max_gzip_mib:g} MiB), {len(files)} file(s)"],
         summary=a.summary, title="Bundle size")
    return 1 if errors else 0


def split_names(text):
    return [n for n in re.split(r"[\s,;]+", text or "") if n]


def cmd_secrets(a):
    names = split_names(a.names)
    bad = [n for n in names if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)]
    if bad:
        print(f"::error::invalid secret name(s): {', '.join(bad)}")
        return 1
    try:
        values = json.loads(os.environ.get("SECRETS_JSON") or "{}")
    except ValueError:
        print("::error::secrets-json is not a JSON object of name to value")  # never echo the value
        return 1
    if not isinstance(values, dict):
        print("::error::secrets-json is not a JSON object of name to value")
        return 1
    missing = [n for n in names if not isinstance(values.get(n), str) or values.get(n) == ""]
    if missing:
        print(f"::error::declared runtime secret(s) missing or empty: {', '.join(missing)}; add them to the GitHub environment and pass them to the workflow")
        return 1
    if a.mask:
        for n in names:
            for line in values[n].splitlines() or [values[n]]:
                if line.strip():
                    print(f"::add-mask::{line}")
        return 0
    sys.stdout.write(json.dumps({n: values[n] for n in names}))
    return 0


def cmd_previous(a):
    text = Path(a.status).read_text(encoding="utf-8")
    start = text.find("{")
    if start < 0:
        return 0  # no deployment yet: nothing to split traffic with
    versions = json.loads(text[start:text.rfind("}") + 1]).get("versions") or []
    if not versions:
        return 0
    best = max(versions, key=lambda v: v.get("percentage", 0))
    if len(versions) > 1:
        print(f"::warning::a gradual deployment is in progress ({len(versions)} versions); the version with most traffic counts as the previous one")
    print(best["version_id"])
    return 0


def cmd_result(a):
    out = {"version-id": "", "preview-url": "", "targets": ""}
    path = Path(a.file)
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") in ("deploy", "version-upload") and e.get("version_id"):
            out["version-id"] = e["version_id"]
        if e.get("type") == "version-upload":
            out["preview-url"] = e.get("preview_alias_url") or ""  # the plain Version URL is off outside previews
        if e.get("type") == "deploy" and e.get("targets"):
            out["targets"] = " ".join(e["targets"])
        if e.get("type") == "version-deploy" and not out["version-id"]:
            traffic = e.get("version_traffic") or {}
            if isinstance(traffic, dict) and traffic:
                out["version-id"] = max(traffic, key=lambda k: traffic[k])
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


def self_test():
    assert strip_jsonc('{"a": "http://x//y", /* c */ "b": [1, 2,], // z\n}') == '{"a": "http://x//y",  "b": [1, 2] \n}'
    good = {"name": "w", "main": "m.js", "compatibility_date": "2026-10-01", "compatibility_flags": ["nodejs_compat"],
            "workers_dev": False, "preview_urls": False, "routes": [{"pattern": "a.example", "custom_domain": True}]}
    assert lint(good, "worker", False, True, False) == ([], [], "https://a.example")
    assert len(lint(dict(good, preview_urls=False), "worker", True, True, False)[0]) == 1
    assert lint(dict(good, workers_dev=True), "worker", False, True, True)[0] == []
    env = resolve_env({"name": "w", "workers_dev": False, "env": {"preview": {"preview_urls": True}}}, "preview")
    assert env["name"] == "w-preview" and env["preview_urls"] is True and env["workers_dev"] is False
    assert split_names("A, B\nC;;") == ["A", "B", "C"]
    print("self-test ok")
    return 0


def main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("lint")
    s.add_argument("--dir", default=".")
    s.add_argument("--config", default="")
    s.add_argument("--env", default="")
    s.add_argument("--mode", choices=["static", "worker"], required=True)
    s.add_argument("--preview", action="store_true")
    s.add_argument("--require-custom-domain", default="true")
    s.add_argument("--allow-workers-dev", default="false")
    s.add_argument("--output", default="")
    s = sub.add_parser("token")
    s.add_argument("--response", action="append", default=[])
    s.add_argument("--warn-days", type=float, default=14)
    s.add_argument("--now", default="")
    s = sub.add_parser("size")
    s.add_argument("--dir", required=True)
    s.add_argument("--max-gzip-mib", type=float, default=10)
    s = sub.add_parser("secrets")
    s.add_argument("--names", default="")
    s.add_argument("--mask", action="store_true")
    s = sub.add_parser("previous")
    s.add_argument("--status", required=True)
    s = sub.add_parser("result")
    s.add_argument("--file", required=True)
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    handlers = {"lint": cmd_lint, "token": cmd_token, "size": cmd_size, "secrets": cmd_secrets, "previous": cmd_previous, "result": cmd_result}
    if a.cmd not in handlers:
        p.print_help()
        return 2
    return handlers[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
