#!/usr/bin/env python3
import os
import re
import subprocess
import sys
from itertools import product
from pathlib import Path

import yaml

EXPR = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")
STARTS = re.compile(r"startsWith\(([^,]+),\s*'([^']*)'\)")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: run_wf.py <workflow.yml>")
    wf_path = Path(sys.argv[1]).resolve()
    if not wf_path.is_file():
        sys.exit(f"missing workflow: {wf_path}")
    src_root = Path(os.environ.get("SRC_ROOT") or wf_path.parents[2]).resolve()
    ctx = {
        "github": {
            "ref": os.environ.get("EVENT_REF") or os.environ.get("GITHUB_REF", ""),
            "ref_name": os.environ.get("EVENT_REF_NAME") or os.environ.get("GITHUB_REF_NAME", ""),
            "actor": os.environ.get("GITHUB_ACTOR", ""),
            "workspace": str(src_root),
        },
        "env": {},
        "steps": {},
        "matrix": {},
    }
    data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    matched = 0
    for name, job in (data.get("jobs") or {}).items():
        if not job_enabled(job.get("if"), ctx):
            continue
        matched += 1
        for matrix in expand_matrix(job.get("strategy") or {}):
            ctx["matrix"] = matrix
            ctx["steps"] = {}
            ctx["env"] = {}
            run_job(name, job, src_root, ctx)
    if matched == 0:
        sys.exit(f"no job matched ref {ctx['github']['ref']}")


def lookup(expr, ctx):
    parts = expr.strip().split(".")
    head = parts[0]
    if head == "secrets":
        return os.environ.get(parts[1], "") if len(parts) > 1 else ""
    if head == "env":
        key = parts[1] if len(parts) > 1 else ""
        return ctx["env"].get(key, os.environ.get(key, ""))
    if head == "github":
        cur = ctx["github"]
        for part in parts[1:]:
            cur = cur.get(part, "") if isinstance(cur, dict) else ""
        return "" if isinstance(cur, dict) else cur
    if head == "steps":
        if len(parts) >= 4 and parts[2] == "outputs":
            return ctx["steps"].get(parts[1], {}).get(parts[3], "")
        return ""
    if head == "matrix":
        cur = ctx["matrix"]
        for part in parts[1:]:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(part, "")
        return "" if isinstance(cur, dict) else cur
    return ""


def interpolate(value, ctx):
    if not isinstance(value, str):
        return value

    def repl(match):
        return str(lookup(match.group(1), ctx))

    return EXPR.sub(repl, value)


def job_enabled(cond, ctx):
    if not cond:
        return True
    text = " ".join(str(cond).split())

    def repl(match):
        current = str(lookup(match.group(1), ctx))
        return "true" if current.startswith(match.group(2)) else "false"

    text = STARTS.sub(repl, text)
    return any(part.strip() == "true" for part in re.split(r"\s*\|\|\s*", text))


def expand_matrix(strategy):
    matrix = strategy.get("matrix")
    if not matrix:
        return [{}]
    keys = list(matrix.keys())
    values = []
    for key in keys:
        item = matrix[key]
        values.append(item if isinstance(item, list) else [item])
    combos = []
    for combo in product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


def parse_kv_file(path):
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def run_uses(step, ctx):
    uses = str(step.get("uses") or "")
    if uses.startswith("actions/checkout"):
        print(f"skip {uses}", flush=True)
        return
    if uses.startswith("docker/login-action"):
        fields = {k: interpolate(v, ctx) for k, v in (step.get("with") or {}).items()}
        registry = fields.get("registry") or "docker.io"
        user = fields.get("username") or ""
        password = fields.get("password") or ""
        print(f"login {registry}", flush=True)
        subprocess.run(
            ["docker", "login", registry, "-u", user, "--password-stdin"],
            input=password,
            text=True,
            check=True,
        )
        return
    print(f"skip unsupported {uses}", flush=True)


def run_step(step, src_root, ctx, gh_env, gh_out):
    name = interpolate(step.get("name") or "step", ctx)
    if step.get("uses"):
        run_uses(step, ctx)
        return
    script = step.get("run")
    if not script:
        return
    script = interpolate(script, ctx)
    env = os.environ.copy()
    env.update({k: str(v) for k, v in ctx["env"].items()})
    for key, value in (step.get("env") or {}).items():
        env[key] = interpolate(str(value), ctx)
    gh_env.write_text("", encoding="utf-8")
    gh_out.write_text("", encoding="utf-8")
    env["GITHUB_ENV"] = str(gh_env)
    env["GITHUB_OUTPUT"] = str(gh_out)
    env["GITHUB_WORKSPACE"] = str(src_root)
    print(f"--> {name}", flush=True)
    subprocess.run(["bash", "-lc", script], cwd=str(src_root), env=env, check=True)
    ctx["env"].update(parse_kv_file(gh_env))
    sid = step.get("id")
    if sid:
        ctx["steps"][sid] = parse_kv_file(gh_out)


def run_job(name, job, src_root, ctx):
    print(f"::group::{name}", flush=True)
    gh_env = src_root / ".gha.env"
    gh_out = src_root / ".gha.out"
    try:
        for step in job.get("steps") or []:
            run_step(step, src_root, ctx, gh_env, gh_out)
    finally:
        print("::endgroup::", flush=True)


if __name__ == "__main__":
    main()
