"""Deploy an exact reviewed commit to ordered Git checkouts.

Site commands are trusted project configuration. An idle command exits zero only
when the site is idle. Tests declare CPU-only and no paid jobs. The operator
must apply the same restriction to trusted idle, backup and publish commands.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import uuid


def validate_sites(sites):
    """Validate site declarations without contacting any site."""
    if not isinstance(sites, list):
        raise ValueError("sites must be an array")
    # Validate all configuration before touching a site.
    names, targets = set(), set()
    for site in sites:
        if not isinstance(site, dict) or site.get("transport") not in ("local", "ssh") or not all(isinstance(site.get(k), str) and site[k] for k in ("name", "repo", "branch", "idle")):
            raise ValueError("each site needs name, transport, repo, branch and idle")
        if site["name"] in names:
            raise ValueError("duplicate site name")
        names.add(site["name"])
        if not Path(site["repo"]).is_absolute():
            raise ValueError("site repo must be an absolute path")
        if site["transport"] == "ssh" and (not isinstance(site.get("host"), str) or not isinstance(site.get("ssh_config"), str) or not site["host"] or not Path(site["ssh_config"]).is_absolute()):
            raise ValueError("ssh requires host and ssh_config")
        if site["transport"] == "ssh" and (not re.fullmatch(r"[A-Za-z0-9._@-]+", site["host"]) or site["host"].startswith("-")):
            raise ValueError("invalid ssh target")
        target = (site["transport"], site.get("host", "") if site["transport"] == "ssh" else "", str(Path(site["repo"]).resolve()) if site["transport"] == "local" else os.path.normpath(site["repo"]))
        if target in targets:
            raise ValueError("duplicate site target")
        targets.add(target)
        if site["branch"].startswith("-") or subprocess.run(["git", "check-ref-format", "refs/heads/" + site["branch"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ValueError("invalid branch")
        tests = site.get("tests")
        if not isinstance(tests, list) or not tests:
            raise ValueError("each site requires at least one test")
        for cmd in tests:
            if not isinstance(cmd, dict) or cmd.get("cpu") is not True or cmd.get("paid") is not False or not isinstance(cmd.get("command"), str) or not cmd["command"].strip():
                raise ValueError("tests must declare cpu=true and paid=false")
        for key in ("publish", "pre_deploy", "source", "source_ref"):
            if key in site and (not isinstance(site[key], str) or not site[key].strip()):
                raise ValueError(f"{key} must be a nonempty string")
        source_ref = site.get("source_ref", "refs/heads/" + site["branch"])
        if not source_ref.startswith("refs/heads/") or subprocess.run(["git", "check-ref-format", source_ref], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ValueError("source_ref must be a branch ref")


def deploy(repo: Path, base: str, candidate: str, sites: list[dict], record: Path) -> dict:
    validate_sites(sites)
    ssh_sites = [site for site in sites if site["transport"] == "ssh"]
    if not ssh_sites:
        return _deploy(repo, base, candidate, sites, record, {}, set())
    # Unix sockets have short path limits; don't inherit a potentially long TMPDIR.
    with tempfile.TemporaryDirectory(prefix="cf-ssh-", dir="/tmp") as directory:
        sockets = {site["name"]: str(Path(directory) / str(i)) for i, site in enumerate(ssh_sites)}
        used = set()
        try:
            return _deploy(repo, base, candidate, sites, record, sockets, used)
        finally:
            for site in ssh_sites:
                if site["name"] in used:
                    argv = ["ssh", "-F", site["ssh_config"], "-o", "ControlPath=" + sockets[site["name"]],
                            "-O", "exit", "--", site["host"]]
                    try:
                        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    except OSError:
                        pass


def _ssh_options(site, sockets):
    return ["-F", site["ssh_config"], "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60", "-o", "ControlPath=" + sockets[site["name"]]]


def _deploy(repo: Path, base: str, candidate: str, sites: list[dict], record: Path, sockets, used) -> dict:
    repo, record = Path(repo).resolve(), Path(record).resolve()
    if record.exists():
        raise ValueError("record already exists; use a new record after inspecting the previous run")
    oid = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
    if not isinstance(base, str) or not isinstance(candidate, str) or not oid.fullmatch(base) or not oid.fullmatch(candidate) or not isinstance(sites, list) or not sites:
        raise ValueError("base and candidate must be full object IDs; sites must be nonempty")
    def local(*args):
        return subprocess.run(args, cwd=repo, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=True).stdout.strip()
    try:
        if local("git", "rev-parse", "--verify", candidate + "^{commit}") != candidate:
            raise RuntimeError("candidate is not an exact commit")
        local("git", "merge-base", "--is-ancestor", base, candidate)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("invalid base or candidate ancestry") from exc

    state = {"base": base, "candidate": candidate, "status": "running", "sites": [
        {"name": s["name"], "steps": []} for s in sites]}
    def save():
        record.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="." + record.name + ".", dir=record.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, record)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    def run(site, command, *args):
        script = 'cd -- "$1" || exit 1\nshift\n' + command
        if site["transport"] == "local":
            argv = ["bash", "-c", script, "deploy", site["repo"], *args]
        else:
            # SSH invokes a remote shell; quote every fixed argument as one word.
            remote = " ".join(shlex.quote(x) for x in ["bash", "-c", script, "deploy", site["repo"], *args])
            used.add(site["name"])
            argv = ["ssh", *_ssh_options(site, sockets), "--", site["host"], remote]
        p = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode:
            raise RuntimeError(f"{site['name']}: command failed ({p.returncode}): {p.stdout.strip()[:500]}")
        return p.stdout.strip()
    def step(i, name, fn):
        entry = {"step": name, "status": "running"}
        state["sites"][i]["steps"].append(entry)
        save()
        try:
            entry["output"] = fn() or ""
            entry["status"] = "ok"
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
            state["status"] = "failed"
            save()
            raise RuntimeError(f"deployment stopped at {state['sites'][i]['name']}: {name}: {str(exc)[:500]}") from exc
        save()
    def check_site(s, expected):
        return run(s, '''
branch=$(git symbolic-ref -q HEAD) || { echo "branch check failed: detached HEAD or Git error"; exit 1; }
if [ "$branch" != "refs/heads/$1" ]; then printf 'wrong branch: %.200s\\n' "$branch"; exit 1; fi
head=$(git rev-parse --verify HEAD) || { echo "HEAD check failed"; exit 1; }
if [ "$head" != "$2" ]; then printf 'baseline mismatch: actual %.128s, expected %.128s\\n' "$head" "$2"; exit 1; fi
status=$(git status --porcelain --untracked-files=all) || { echo "git status check failed"; exit 1; }
if [ -n "$status" ]; then echo "worktree is dirty"; exit 1; fi
bash -c "$3" || { echo "busy or idle check failed"; exit 1; }''', s["branch"], expected, s["idle"])
    def preflight(s):
        return check_site(s, base)
    save()
    for i, site in enumerate(sites):
        step(i, "preflight", lambda s=site: preflight(s))
    for i, site in enumerate(sites):
        step(i, "recheck", lambda s=site: preflight(s))
        if site.get("pre_deploy"):
            step(i, "pre_deploy", lambda s=site: run(s, 'bash -c "$1"', s["pre_deploy"]))
            step(i, "recheck_after_backup", lambda s=site: preflight(s))
        incoming = "refs/codex-flow/incoming/" + uuid.uuid4().hex
        state["sites"][i]["incoming_ref"] = incoming
        save()
        def transfer(s=site):
            if s.get("source"):
                return run(s, 'git fetch --no-tags --no-recurse-submodules -- "$1" "$2:$3"', s["source"], s.get("source_ref", "refs/heads/" + s["branch"]), incoming)
            dest = s["repo"] if s["transport"] == "local" else s["host"] + ":" + s["repo"]
            env = os.environ.copy()
            if s["transport"] == "ssh":
                used.add(s["name"])
                env["GIT_SSH_COMMAND"] = shlex.join(["ssh", *_ssh_options(s, sockets)])
            p = subprocess.run(["git", "push", "--no-follow-tags", "--recurse-submodules=no", "--", dest, candidate + ":" + incoming], cwd=repo,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            if p.returncode:
                raise RuntimeError(p.stdout.strip()[:500])
            return p.stdout.strip()
        step(i, "transfer", transfer)
        step(i, "verify_incoming", lambda s=site: run(s, 'test "$(git rev-parse --verify "$1^{commit}")" = "$2"', incoming, candidate))
        step(i, "recheck_before_update", lambda s=site: preflight(s))
        step(i, "update", lambda s=site: run(s, 'git merge --ff-only --no-overwrite-ignore "$1"', candidate))
        step(i, "verify", lambda s=site: check_site(s, candidate))
        for n, test in enumerate(site.get("tests", []), 1):
            step(i, f"test_{n}", lambda t=test, s=site: run(s, 'bash -c "$1"', t["command"]))
        if site.get("publish"):
            step(i, "verify_before_publish", lambda s=site: check_site(s, candidate))
            step(i, "publish", lambda s=site: run(s, 'bash -c "$1"', s["publish"]))
        step(i, "verify_final", lambda s=site: check_site(s, candidate))
        step(i, "cleanup_ref", lambda s=site: run(s, 'git update-ref -d "$1" "$2"', incoming, candidate))
    state["status"] = "complete"
    save()
    return state
