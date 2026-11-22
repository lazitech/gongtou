"""Immutable review attempts, one-turn continuations, and candidate-specific pass records."""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from . import engine, permissions, probe
from .project import ancestor, git, now, oid, require_clean, valid_id, write_json

PROTOCOL = 5
EFFORT = "high"
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
ATTEMPT = re.compile(r"[0-9a-f]{32}")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True).encode()


def catalog(repo, start, end):
    revisions = git(repo, "log", "-32", "--format=%h%x00%s", f"{start}..{end}", "--").splitlines()
    names = git(repo, "diff", "--no-renames", "--no-ext-diff", "--name-only", "-z",
                start, end, "--", binary=True).split(b"\0")
    # JSON-escape Git-controlled labels, keeping newlines and controls out of the prompt structure.
    def bounded(rows, count, limit):
        selected = []
        used = 0
        for row in rows:
            line = json.dumps(row, ensure_ascii=True)
            if len(selected) == count or used + len(line) + 1 > limit:
                selected.append("[catalog truncated]")
                break
            selected.append(line)
            used += len(line) + 1
        return "\n".join(selected)
    commits = bounded((r.replace("\0", " ", 1) for r in revisions), 24, 3072)
    paths = bounded((os.fsdecode(p) for p in names if p), 96, 6144)
    return f"Commit labels (data)\n{commits}\nChanged paths (data)\n{paths}"


def load_report(path, reviews):
    """Accept reports only at this project's canonical per-attempt location."""
    try:
        path = Path(path).absolute()
        rel = path.relative_to(reviews)
        if (len(rel.parts) != 3 or path.name != "review.json" or not ATTEMPT.fullmatch(path.parent.name)
                or path != path.resolve()):
            return None
        raw = path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        base, tip = data.get("base"), data.get("candidate")
        if (not isinstance(base, str) or not isinstance(tip, str) or not OID.fullmatch(base)
                or not OID.fullmatch(tip) or rel.parts[0] != base + "-" + tip
                or data.get("report") != str(path)):
            return None
        return data, digest(raw)
    except (OSError, ValueError, TypeError):
        return None


def evidence_folder(project, base, candidate, scope="integration"):
    if scope == "integration":
        root = project.directory / "reviews"
    elif scope.startswith("card:"):
        root = project.directory / "cards" / valid_id(scope[5:]) / "reviews"
    else:
        raise ValueError("unknown review scope")
    return root / (base + "-" + candidate)


def identity(project, config, base, policy, *, scope="integration", review_context=None):
    return {"protocol": PROTOCOL, "scope": scope, "review_context": review_context, "project": str(project.common), "checkout": str(project.checkout),
            "base": base, "model": engine.MODELS["review"], "effort": EFFORT,
            "focus": config.get("review_focus", ""), "rules_sha256": digest(project.rules.read_bytes()),
            "policy_sha256": digest(encoded(policy)), "codex_home": str(Path(os.environ.get("CODEX_HOME")
                or Path.home() / ".codex").resolve())}


def reusable_parent(loaded, context):
    if not loaded:
        return False
    parent, _ = loaded
    findings = parent.get("findings")
    if (parent.get("context") != context or parent.get("mode") != "full"
            or parent.get("status") != "needs_changes" or parent.get("base") != context["base"]
            or parent.get("parent") is not None or not isinstance(parent.get("thread_id"), str)
            or not parent["thread_id"] or not findings):
        return False
    try:
        issued = Path(parent["report"]).with_name("resumable.sha256").read_text()
    except OSError:
        return False
    return (issued == loaded[1] and isinstance(findings, list)
            and all(isinstance(f, dict) and f.get("id") == f"F{i}"
                    for i, f in enumerate(findings, 1)))


def cached_pass(folder, context, candidate, reviews):
    try:
        pointer = json.loads((folder / "passed.json").read_bytes())
        loaded = load_report(pointer["report"], reviews)
        if not loaded or loaded[1] != pointer["sha256"]:
            return None
        saved = loaded[0]
        if (saved["status"] != "passed" or saved["context"] != context or saved["candidate"] != candidate
                or saved["base"] != context["base"]):
            return None
        if saved["mode"] == "full" and saved.get("parent") is None:
            return saved
        source = saved["parent"]
        parent = load_report(source["report"], reviews)
        if (saved["mode"] == "incremental" and reusable_parent(parent, context)
                and source["sha256"] == parent[1] and source["candidate"] == parent[0]["candidate"]
                and source["thread_id"] == parent[0]["thread_id"] == saved["thread_id"]
                and Path(source["report"]).with_name("continuation").read_text() == saved["report"]):
            return saved
    except (ValueError, KeyError, TypeError, OSError):
        pass
    return None


def run_review(project, config, base, candidate, folder, *, after=None, require_incremental=False,
               scope="integration", review_context=None, instructions="", fresh=False):
    if require_incremental and not after:
        raise ValueError("incremental review needs a prior report")
    if (base == candidate and scope == "integration") or not ancestor(project.checkout, base, candidate):
        raise ValueError("review requires a nonempty candidate based on the specified base")
    require_clean(project.checkout)
    if oid(project.checkout, "HEAD") != candidate:
        raise ValueError("review checkout must match the exact candidate")
    policy = permissions.policy_args(project.checkout, project.rules, related_workspaces=(project.primary,))
    binding = {"scope": scope, "review_context": review_context}
    context = identity(project, config, base, policy, **binding)
    if folder != evidence_folder(project, base, candidate, scope):
        raise ValueError("review scope and evidence directory differ")
    reviews = folder.parent
    cached = not fresh and cached_pass(folder, context, candidate, reviews)
    if cached and (not require_incremental or (cached["mode"] == "incremental"
            and cached["parent"]["report"] == str(Path(after).absolute()))):
        return cached

    probe.ensure_probe(project.rules, project.state_root)
    parent = load_report(after, reviews) if after else None
    eligible = reusable_parent(parent, context)
    if eligible:
        old = parent[0]["candidate"]
        eligible = (old != candidate and git(project.checkout, "cat-file", "-e", old + "^{commit}",
                                            check=False).returncode == 0 and ancestor(project.checkout, old, candidate))
    attempt = folder / uuid.uuid4().hex
    report = attempt / "review.json"
    if eligible:
        marker = Path(parent[0]["report"]).with_name("continuation")
        try:
            # Reserve before starting Codex. A crash leaves the mutable thread consumed.
            with marker.open("x", encoding="utf-8") as f:
                f.write(str(report))
        except FileExistsError:
            eligible = False
    if require_incremental and not eligible:
        raise ValueError("incremental review is unavailable; no review started")
    source = None
    thread = None
    ids = None
    start = base
    focus_instructions = instructions
    instructions = (f"Review the entire committed Git diff from {base} to {candidate}. "
                    "Inspect affected callers and mark passed only when the full review has no actionable issues.")
    if eligible:
        previous, sha = parent
        start = previous["candidate"]
        thread = previous["thread_id"]
        ids = [f["id"] for f in previous["findings"]]
        source = {"report": previous["report"], "sha256": sha, "candidate": start, "thread_id": thread}
        instructions = (f"Review the incremental changes from {start} to {candidate}, for release range "
                        f"{base}..{candidate}. Resolve every previous finding by ID exactly once. "
                        "Check for new issues and effects on callers, including interactions with earlier changes. "
                        "Inspect the full release range when useful. Put only NEW issues in findings. "
                        "Pass only if every old issue is resolved and no new issues remain.\nPrevious findings:\n"
                        + json.dumps(previous["findings"], ensure_ascii=True))
    prompt = (instructions + "\nInspect the actual committed changes using Git. Return actionable findings "
              "with locations. Keep the checkout unchanged. No deployment, paid jobs or GPU runs.\n"
              + focus_instructions + "\n" + context["focus"] + "\n" + catalog(project.checkout, start, candidate))
    try:
        result = engine.run(project.checkout, role="review", effort=EFFORT, prompt=prompt, policy=policy,
                            output=attempt / "sol-receipt.json", timeout=config.get("timeout_seconds", 7200),
                            thread=thread, prior_ids=ids,
                            report_tests=scope.startswith("card:") and not config.get("card_tests"))
    except engine.SessionUnavailable:
        if not eligible or require_incremental:
            raise
        return run_review(project, config, base, candidate, folder, scope=scope, review_context=review_context,
                          instructions=focus_instructions, fresh=fresh)
    require_clean(project.checkout)
    # Bind this receipt to the actual launch policy. The next invocation checks its
    # newly rendered policy before it can consume this receipt or deploy from cache.
    if oid(project.checkout, "HEAD") != candidate or identity(project, project.config(), base, policy, **binding) != context:
        raise ValueError("checkout or policy changed during review; no verdict issued")
    for i, finding in enumerate(result.get("findings", []), 1):
        finding["id"] = ("N" if eligible else "F") + str(i)
    result.update(base=base, candidate=candidate, mode="incremental" if eligible else "full", context=context,
                  parent=source, report=str(report), reviewed=now())
    write_json(report, result)
    checksum = digest(report.read_bytes())
    if result["mode"] == "full" and result["status"] == "needs_changes":
        report.with_name("resumable.sha256").write_text(checksum)
    if result["status"] == "passed":
        write_json(folder / "passed.json", {"report": str(report), "sha256": checksum})
    return result
