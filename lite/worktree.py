"""Prepare a new card worktree with untracked files shared from the primary checkout.

A fresh `git worktree add` contains only tracked files. Projects often need
untracked environments (virtualenvs, toolchains, header directories) to build
and test. The optional [worktree] table in the project TOML lists them:

    link  - paths symlinked to the primary checkout (shared, read-only by convention)
    copy  - small paths copied from the primary checkout
    setup - one shell command run in the card worktree, inside the Codex sandbox

Build outputs derived from tracked sources belong in `setup`, never in link or
copy, so a card that edits those sources is tested against a fresh build.
"""
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

from . import engine, permissions
from .project import git, require_clean

KEYS = {"link", "copy", "setup"}
EXCLUDE_MARKER = "# codex-flow-lite: card worktree links and copies"


def spec(config):
    table = config.get("worktree") or {}
    return {"link": list(table.get("link", [])), "copy": list(table.get("copy", [])),
            "setup": table.get("setup")}


def validate(config):
    """Shape check without touching the filesystem."""
    if "worktree" not in config:
        return
    table = config["worktree"]
    if not isinstance(table, dict) or set(table) - KEYS:
        raise ValueError("worktree must be a table with only link, copy and setup")
    seen = set()
    for mode in ("link", "copy"):
        paths = table.get(mode, [])
        if not isinstance(paths, list):
            raise ValueError(f"worktree.{mode} must be an array of relative paths")
        for rel in paths:
            if not isinstance(rel, str) or not rel or rel.startswith("/") or "\x00" in rel:
                raise ValueError(f"worktree.{mode} entries must be nonempty relative paths")
            parts = PurePosixPath(rel).parts
            if ".." in parts or "." in parts or str(PurePosixPath(rel)) != rel.rstrip("/") or parts[0] == ".git":
                raise ValueError(f"worktree.{mode} entry must be a normalized path inside the checkout: {rel}")
            if rel.rstrip("/") in seen:
                raise ValueError(f"worktree path listed twice: {rel}")
            seen.add(rel.rstrip("/"))
    if "setup" in table and (not isinstance(table["setup"], str) or not table["setup"].strip()):
        raise ValueError("worktree.setup must be a nonempty command string")


def _sources(project, config):
    """Yield (mode, rel, source) after checking each source on disk."""
    root = Path(project.primary)
    denied = permissions.credential_matcher(root, project.rules)
    for mode in ("link", "copy"):
        for rel in spec(config)[mode]:
            rel = rel.rstrip("/")
            source = root / rel
            if not os.path.lexists(source):
                raise ValueError(f"worktree.{mode} source is missing in the primary checkout: {source}")
            if denied(source) or denied(Path(os.path.realpath(source))):
                raise ValueError(f"worktree.{mode} path matches a credential rule: {rel}")
            if git(root, "ls-files", "-z", "--", rel, binary=True):
                raise ValueError(f"worktree.{mode} path is tracked by Git; only untracked files need sharing: {rel}")
            yield mode, rel, source


def check_sources(project, config):
    """validate-config: confirm every link and copy source exists and is shareable."""
    return [{"mode": mode, "path": rel} for mode, rel, _ in _sources(project, config)]


def _hide(project, wt, placed):
    """Keep placed paths out of `git add -A`.

    A symlink does not match a directory-only pattern such as `.venv/`, so it
    would otherwise appear untracked and be committed with the card.
    """
    visible = [rel for rel in placed
               if git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", rel, binary=True)]
    if not visible:
        return []
    exclude = Path(project.common) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    lines = text.splitlines()
    added = [rel for rel in visible if "/" + rel not in lines]
    if added:
        block = ("" if not text or text.endswith("\n") else "\n")
        if EXCLUDE_MARKER not in lines:
            block += EXCLUDE_MARKER + "\n"
        exclude.write_text(text + block + "".join("/" + rel + "\n" for rel in added), encoding="utf-8")
    still = [rel for rel in visible
             if git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", rel, binary=True)]
    if still:
        raise ValueError(f"cannot hide placed worktree paths from Git: {still}")
    return added


def prepare(project, config, wt, log):
    """Place links and copies, then run setup. Raises on any failure."""
    wt = Path(wt)
    table = spec(config)
    result = {"linked": [], "copied": [], "excluded": [], "setup": None}
    if not table["link"] and not table["copy"] and not table["setup"]:
        return result
    denied = permissions.credential_matcher(wt, project.rules)
    placed = []
    for mode, rel, source in _sources(project, config):
        target = wt / rel
        if denied(target):
            raise ValueError(f"worktree.{mode} path matches a credential rule: {rel}")
        if os.path.lexists(target):
            raise ValueError(f"worktree.{mode} target already exists in the card checkout: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "link":
            target.symlink_to(source.absolute(), target_is_directory=source.is_dir())
            result["linked"].append(rel)
        elif source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, symlinks=True)
            result["copied"].append(rel)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
            result["copied"].append(rel)
        placed.append(rel)
    result["excluded"] = _hide(project, wt, placed)
    if table["setup"]:
        command = table["setup"]
        policy = permissions.policy_args(wt, project.rules, related_workspaces=(project.checkout, project.primary))
        args = ["codex", "sandbox", *policy, "--", "/bin/bash", "-c", command]
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as out:
            try:
                code = engine._execute(args, prompt="", stdout=out, stderr=out, cwd=wt, env=None,
                                       timeout=config.get("timeout_seconds", 7200))
            except subprocess.TimeoutExpired:
                code = None
        result["setup"] = {"command": command, "exit_code": code, "log": str(log)}
        if code != 0:
            raise ValueError(f"worktree.setup exited {code}; log: {log}")
    try:
        require_clean(wt)
    except ValueError:
        raise ValueError("worktree setup left tracked changes or untracked files that Git does not ignore; "
                         "ignore its outputs in .gitignore") from None
    if table["setup"]:
        # Ignored files and directories present right after setup are launcher output.
        names = git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", binary=True)
        # A directory holding only placed paths collapses into one entry; drop such
        # ancestors so they never mark unrelated later files as launcher-owned.
        outputs = {os.fsdecode(p).rstrip("/") for p in names.split(b"\0") if p} - set(placed)
        result["setup_outputs"] = sorted(o for o in outputs
                                         if not any(rel.startswith(o + "/") for rel in placed))
    return result


def owned(card):
    """Paths the launcher placed or created in a card worktree."""
    setup = card.get("worktree_setup") or {}
    paths = setup.get("linked", []) + setup.get("copied", []) + setup.get("setup_outputs", [])
    return [p for p in paths if isinstance(p, str) and p]


def unowned_ignored(wt, card):
    """Ignored files in a card worktree that the launcher did not place."""
    roots = owned(card)
    names = git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", binary=True).split(b"\0")
    return [name for name in (os.fsdecode(p) for p in names if p)
            if not any(name == rel or name.startswith(rel + "/") for rel in roots)]
