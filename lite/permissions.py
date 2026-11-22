"""Render editable, path-only credential exclusions for Codex and Git.

These are accidental-read guards, not a guarantee against every credential name.
Rules are evaluated without opening the protected files.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shlex
from pathlib import Path
import tomllib
import re


def _matches(pattern: str, path: str) -> bool:
    # **/ also matches zero directories, unlike fnmatch's treatment of '/'.
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if "**/" in pattern:
        return _matches(pattern.replace("**/", "", 1), path)
    if pattern.endswith("/**"):
        return path == pattern[:-3] or path.startswith(pattern[:-2])
    return False


def _patterns(worktree: Path, rules: Path, env=None, related_workspaces=()) -> list[str]:
    env = os.environ if env is None else env
    try:
        data = tomllib.loads(rules.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid credential rules: {exc}") from exc
    if set(data) != {"deny"}:
        raise ValueError("credential rules require only the deny key")
    home = os.path.abspath(env["HOME"])
    codex = os.path.abspath(env.get("CODEX_HOME", home + "/.codex"))
    values = {"home": home, "codex_home": codex, "workspace": os.path.abspath(worktree)}
    patterns = data["deny"]
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError("deny must be an array of paths")
    try:
        result = [p.format_map(values) for p in patterns]
        for workspace in dict.fromkeys(os.path.abspath(p) for p in related_workspaces):
            if workspace != values["workspace"]:
                related = {**values, "workspace": workspace}
                result.extend(p.format_map(related) for p in patterns if "{workspace}" in p)
    except (KeyError, ValueError, IndexError, AttributeError) as exc:
        raise ValueError(f"invalid credential path template: {exc}") from exc
    if any(not p.startswith("/") or "\x00" in p or ".." in p.split("/") for p in result):
        raise ValueError("credential patterns must be absolute and contain no parent traversal")
    ssh_glob = home + "/.ssh/id_*"
    if ssh_glob in result:
        result.remove(ssh_glob)
        # The final b in .pub keeps public keys readable. Existing names ending
        # in b are separately enumerated below; future such names need a rule.
        result.append(home + "/.ssh/id_*[!b]")
        names = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"}
        try:
            names.update(p.name for p in (Path(home) / ".ssh").iterdir()
                         if p.name.startswith("id_") and not p.name.endswith(".pub"))
        except FileNotFoundError:
            pass
        result.extend(home + "/.ssh/" + name for name in sorted(names))
    return list(dict.fromkeys(result))


def credential_matcher(worktree: Path, rules: Path, *, env=None):
    patterns = _patterns(worktree, rules, env)
    def match(path: Path) -> bool:
        value = os.path.abspath(path)
        candidates = (Path(value), *Path(value).parents)
        # Check each ancestor so directory rules and globbed directories apply.
        return any(_matches(p, str(candidate)) for p in patterns
                   for candidate in candidates)
    return match


def denied_path(path: Path, worktree: Path, rules: Path, *, env=None) -> bool:
    return credential_matcher(worktree, rules, env=env)(path)


def stage_exclusions(worktree: Path, rules: Path, *, env=None, candidates=None) -> list[str]:
    root = os.path.abspath(worktree).rstrip("/")
    def exact_exclusions():
        if candidates is None:
            raise ValueError("credential rule overlaps worktree; provide candidate paths")
        match = credential_matcher(worktree, rules, env=env)
        result = []
        for candidate in candidates() if callable(candidates) else candidates:
            path = os.path.abspath(os.path.join(root, os.fspath(candidate)))
            if not path.startswith(root + "/"):
                raise ValueError("candidate must be inside worktree")
            if match(Path(path)):
                result.append(":(top,exclude,literal)" + os.path.relpath(path, root))
        return list(dict.fromkeys(result))
    exclusions = []
    for pattern in _patterns(worktree, rules, env):
        if pattern.startswith(root + "/"):
            exclusions.append(":(top,exclude,glob)" + pattern[len(root) + 1:])
        else:
            # An ancestor or an ambiguous glob overlapping the root can cover
            # any file; fail closed rather than silently stage it.
            prefix = re.split(r"[?*\[]", pattern, maxsplit=1)[0].rstrip("/")
            if any(_matches(pattern, str(p)) for p in (Path(root), *Path(root).parents)) or (
                prefix and (root == prefix or root.startswith(prefix + "/")) and prefix != pattern.rstrip("/")
            ):
                return exact_exclusions()
    return exclusions


def policy_args(worktree: Path, rules: Path, *, env=None, related_workspaces=()) -> list[str]:
    env = os.environ if env is None else env
    patterns = _patterns(worktree, rules, env, related_workspaces)
    q = lambda x: json.dumps(x, ensure_ascii=False)
    root = os.path.abspath(worktree).rstrip("/")
    ws = [p[len(root) + 1:] for p in patterns if p.startswith(root + "/")
          and (any(c in p for c in "*?[") or os.path.lexists(p))]
    # Bubblewrap cannot mask a missing exact path on a read-only parent.
    # Missing paths are omitted, so newly created credentials are not covered.
    outside = [p for p in patterns if not p.startswith(root + "/")
               and (any(c in p for c in "*?[") or os.path.lexists(p))]
    ws_rules = ",".join(q(p) + '="none"' for p in ws)
    entries = {os.path.abspath(env["HOME"]): "write", "/tmp": "write"}
    entries.update((p, "none") for p in outside)
    other = ",".join(q(p) + "=" + q(access) for p, access in entries.items())
    policy = ('permissions.lite={filesystem={":root"="read",":workspace_roots"={"."="write"'
              + ("," + ws_rules if ws_rules else "") + '},":tmpdir"="write",'
              + other + '},network={enabled=true}}')
    ssh_config = Path(env["HOME"]) / ".ssh/config"
    ssh = "ssh -F " + shlex.quote(str(ssh_config) if ssh_config.is_file() else "/dev/null")
    args = ["-c", policy, "-c", 'default_permissions="lite"',
            "-c", 'approval_policy="never"',
            "-c", 'web_search="live"',
            "-c", 'features.multi_agent=true',
            "-c", 'shell_environment_policy.set.GIT_SSH_COMMAND=' + q(ssh),
            "-c", 'shell_environment_policy.set.RSYNC_RSH=' + q(ssh)]
    if env.get("SSH_AUTH_SOCK"):
        args.extend(["-c", 'shell_environment_policy.set.SSH_AUTH_SOCK=' + q(env["SSH_AUTH_SOCK"])])
    return args
