#!/usr/bin/env python3
"""Runs the composite steps of action.yml the way the runner does, with fake `curl` and `pnpm exec wrangler`
(test/stub), and checks which Wrangler commands each scenario issues and which outputs it sets. No network.

Only the expression forms action.yml uses are understood: inputs.*, github.action_path, runner.temp,
steps.<id>.outputs.*, string comparison with == and !=, and &&.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
BASH = os.environ.get("BASH_BIN") or shutil.which("bash")
CTX = re.compile(r"(inputs|github|runner|steps\.[\w-]+\.outputs)\.([\w-]+)")


def run(inputs, status=None, token="valid.json", config="good-worker.jsonc"):
    tmp = Path(tempfile.mkdtemp())
    work, temp = tmp / "work", tmp / "temp"
    work.mkdir(); temp.mkdir()
    shutil.copy(ROOT / "test/config" / config, work / "wrangler.jsonc")
    log, summary = tmp / "stub.log", tmp / "summary.md"
    log.touch()
    values = {k: str(v.get("default", "")) for k, v in ACTION["inputs"].items()}
    values.update({"artifact-dir": "work"}, **inputs)
    steps = {}

    def lookup(m):
        ctx, key = m.group(1), m.group(2)
        if ctx == "inputs":
            return values[key]
        if ctx == "github":
            return {"action_path": ROOT.as_posix()}[key]
        if ctx == "runner":
            return {"temp": temp.as_posix()}[key]
        return steps.get(ctx.split(".")[1], {}).get(key, "")

    def expand(text):
        return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: CTX.sub(lookup, m.group(1)), str(text))

    def condition(expr):
        py = CTX.sub(lambda m: repr(lookup(m)), expr).replace("&&", " and ")
        return eval(py, {"__builtins__": {}})  # only literals and comparisons remain

    env_base = dict(os.environ, PATH=os.pathsep.join([str(ROOT / "test/stub"), os.environ["PATH"]]), STUB_LOG=log.as_posix(),
                    STUB_TOKEN=(ROOT / "test/token" / token).as_posix(), STUB_STATUS=(ROOT / "test/wrangler" / status).as_posix() if status else "",
                    RUNNER_TEMP=temp.as_posix(), GITHUB_STEP_SUMMARY=summary.as_posix(), GITHUB_REPOSITORY="acme/site",
                    GITHUB_SHA="0123456789abcdef0123", GITHUB_RUN_ID="42")
    for step in ACTION["runs"]["steps"]:
        if "if" in step and not condition(step["if"]):
            continue
        out = tmp / "github_output"
        out.write_text("")
        env = dict(env_base, GITHUB_OUTPUT=out.as_posix(), **{k: expand(v) for k, v in (step.get("env") or {}).items()})
        script = tmp / "step.sh"
        script.write_text(expand(step["run"]), newline="\n")
        cwd = tmp / expand(step.get("working-directory", "."))
        proc = subprocess.run([BASH, "--noprofile", "--norc", "-eo", "pipefail", script.as_posix()], cwd=cwd, env=env, capture_output=True, text=True)
        steps[step.get("id", "")] = dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l)
        if proc.returncode:
            return False, steps, log.read_text(), proc.stdout + proc.stderr
    return True, steps, log.read_text(), ""


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name}\n{detail}")
        sys.exit(1)


SECRETS = '{"SECURITY__HMAC_KEY": "0123456789abcdef0123456789abcdef", "PRD_ONLY": "never-sent"}'
NEW, PREV = "99999999-9999-4999-8999-999999999999", "11111111-1111-4111-8111-111111111111"

ok, steps, log, err = run({"mode": "worker", "percentage": "25", "secret-names": "SECURITY__HMAC_KEY", "secrets-json": SECRETS,
                           "account-id": "acc", "api-token": "tok"}, status="status-single.json")
check("gradual worker deploy", ok, err)
check("token verified on both endpoints", log.count("curl https://api.cloudflare.com/client/v4/") == 2, log)
check("dry run bundles before the upload", log.index("deploy --dry-run") < log.index("versions upload"), log)
check("only the declared secret rides on the version", "secrets SECURITY__HMAC_KEY\n" in log, log)
check("split with the previous version", f"versions deploy {NEW}@25% {PREV}@75% -y" in log, log)
check("routes applied after versions deploy", log.rstrip().endswith("triggers deploy (cwd work)"), log)
check("outputs", steps["result"] == {"version-id": NEW, "preview-url": "", "url": "https://www.example.com"} and steps["publish"]["previous-version-id"] == PREV, steps)

ok, steps, log, err = run({"mode": "worker", "environment-name": "preview", "preview-alias": "pr-7", "account-id": "acc", "api-token": "tok"},
                          status="status-single.json")
check("preview", ok, err)
check("preview uploads with the alias and deploys nothing", "--preview-alias pr-7" in log and "versions deploy" not in log and "triggers" not in log, log)
check("preview uses the Wrangler environment", "versions upload --preview-alias pr-7 --message acme/site@0123456789ab run 42 --env preview" in log, log)
check("preview outputs", steps["result"]["preview-url"] == "https://pr-7-site-preview.acme.workers.dev" and steps["result"]["url"] == "", steps)

ok, steps, log, err = run({"mode": "worker", "percentage": "50", "account-id": "acc", "api-token": "tok"})
check("first deploy", ok, err)
check("first deploy runs wrangler deploy", "\nwrangler deploy --message" in "\n" + log and "versions upload" not in log, log)

ok, steps, log, err = run({"mode": "static", "account-id": "acc", "api-token": "tok"}, status="status-single.json", config="good-static.jsonc")
check("static", ok, err)
check("static deploy, no bundle size", "deploy --dry-run" not in log and "\nwrangler deploy --message" in "\n" + log, log)
check("static outputs", steps["result"]["version-id"] == "88888888-8888-4888-8888-888888888888" and steps["result"]["url"] == "https://docs.example.com", steps)

ok, steps, log, err = run({"mode": "worker", "rollback-to": PREV, "account-id": "acc", "api-token": "tok"}, status="status-split.json",
                          config="bad-worker.jsonc")
check("rollback", ok, err)
check("rollback only moves traffic", f"versions deploy {PREV}@100% -y" in log and "upload" not in log and "dry-run" not in log, log)
check("rollback outputs", steps["result"]["version-id"] == PREV, steps)

ok, steps, log, err = run({"mode": "worker", "account-id": "acc", "api-token": "tok"}, status="status-single.json", config="bad-worker.jsonc")
check("bad configuration stops before Wrangler", not ok and "wrangler" not in log and "workers_dev" in err, log + err)

ok, steps, log, err = run({"mode": "worker", "secret-names": "SECURITY__HMAC_KEY,MISSING", "secrets-json": SECRETS, "account-id": "acc", "api-token": "tok"},
                          status="status-single.json")
check("missing secret stops before Wrangler", not ok and "wrangler" not in log and "MISSING" in err and "0123456789abcdef0123456789abcdef" not in err.replace("::add-mask::0123456789abcdef0123456789abcdef", ""), err)

ok, steps, log, err = run({"mode": "worker", "account-id": "acc", "api-token": "tok"}, token="invalid.json")
check("invalid token stops before Wrangler", not ok and "wrangler" not in log and "invalid, revoked or expired" in err, err)

ok, steps, log, err = run({"mode": "static", "percentage": "10", "account-id": "acc", "api-token": "tok"})
check("static refuses percentage", not ok and "need mode worker" in err, err)
print("action tests ok")
