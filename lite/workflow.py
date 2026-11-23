"""Local card lifecycle and publication of an explicitly reviewed Git commit."""
import json
import os
from pathlib import Path
import time
import uuid

from . import deploy as delivery
from . import card_review, engine, permissions, probe, reviews, coverage
from .project import ancestor, git, lock, now, oid, read_card, require_clean, valid_id, write_json


def card_record(project, key):
    f = project.directory / "cards" / valid_id(key) / "record.json"
    if not f.is_file():
        raise ValueError(f"unknown card: {key}")
    return f, json.loads(f.read_text(encoding="utf-8"))


def check_checkout(wt, branch, head):
    if git(wt, "symbolic-ref", "-q", "HEAD") != "refs/heads/" + branch or oid(wt, "HEAD") != head:
        raise ValueError(f"card branch or HEAD changed in {wt}; worktree retained for inspection")


def protect_changed_names(wt, paths, denied):
    for name in set(paths.split(b"\0")):
        if name and denied(wt / os.fsdecode(name)):
            raise ValueError("selected changes include a credential-excluded path; inspect locally")


def finish_card(wt, rec, rules):
    check_checkout(wt, rec["branch"], rec["head"])
    if git(wt, "ls-files", "--unmerged", "-z", binary=True):
        raise ValueError("unresolved merge entries; resolve and stage them in the card worktree before resuming")
    def candidate_names():
        changed = git(wt, "ls-files", "--modified", "--deleted", "--others", "--exclude-standard",
                      "-z", binary=True)
        return (os.fsdecode(name) for name in changed.split(b"\0") if name)

    excludes = permissions.stage_exclusions(wt, rules, candidates=candidate_names)
    denied = permissions.credential_matcher(wt, rules)
    # Check only names before staging and before committing; the host never adds
    # the contents of excluded paths (including files already staged by the worker).
    def staged_names():
        return git(wt, "diff", "--cached", "--no-renames", "--name-only", "-z", binary=True)
    protect_changed_names(wt, staged_names(), denied)
    git(wt, "add", "-A", "--", ".", *excludes)
    changed = staged_names()
    protect_changed_names(wt, changed, denied)
    if not changed:
        return None
    git(wt, "commit", "-m", f"Complete card {rec['id']}", "-m", f"Codex-Card: {rec['id']}")
    return oid(wt, "HEAD")


def task(project, role, card, *, base=None, resume_id=None):
    start = time.monotonic()
    config = project.config()
    key, effort, body = read_card(card, existing_id=resume_id)
    folder = project.directory / "cards" / key
    with lock(project.directory / "locks" / ("card-" + key + ".lock")):
        path = folder / "record.json"
        if resume_id is not None:
            _, rec = card_record(project, key)
            if not rec.get("thread_id") or rec.get("cleaned"):
                raise ValueError("no live worktree and resumable thread for this card")
            wt = Path(rec["worktree"])
            check_checkout(wt, rec["branch"], rec["head"])
        else:
            if folder.exists():
                raise ValueError(f"card ID already exists: {key}; use resume or a new ID")
            branch = "codex-flow/card/" + key
            base_oid = oid(project.checkout, base or config["base"])
            wt = project.directory / "worktrees" / key
            rec = {"id": key, "base": base_oid, "head": base_oid, "branch": branch,
                   "role": role, "worktree": str(wt), "created": now(), "attempts": 0,
                   "thread_id": None, "commit": None, "status": "new"}
        configured_python = config.get("interpreter")
        if configured_python is not None and (not isinstance(configured_python, str)
                or not Path(configured_python).is_absolute() or not os.access(configured_python, os.X_OK)):
            raise ValueError("configured interpreter must be an absolute, executable local path")
        probe.ensure_probe(project.rules, project.state_root)
        previous_review = rec.get("card_review", {})
        parent_review = rec.get("review_parent")
        if previous_review.get("review_status") == "needs_changes":
            parent_review = previous_review.get("report")
        if parent_review:
            rec["review_parent"] = parent_review
        # A running or resumed card never advertises a previous pass as current.
        rec.pop("card_review", None)
        rec.update(status="running", attempts=rec["attempts"] + 1, timeout_seconds=config.get("timeout_seconds", 7200),
                   effort=effort or rec.get("effort") or ("medium" if rec["role"] == "astra" else "high"))
        attempt = folder / f"attempt-{rec['attempts']}"
        attempt.mkdir(parents=True, exist_ok=False)
        write_json(path, rec)
        try:
            if resume_id is None:
                wt.parent.mkdir(parents=True, exist_ok=True)
                git(project.checkout, "worktree", "add", "-b", rec["branch"], wt, rec["base"])
            (attempt / "card.md").write_text(body, encoding="utf-8")
            prompt = ("Implement this card in the current task worktree. Leave the branch and HEAD unchanged; "
                      "the launcher commits completed edits. Follow project instructions. Do not read credentials. "
                      "Leave deployment to Claude via codex-flow-lite ship. GPU experiments and paid API jobs require separate user approval. "
                      "Do not start or stop other users' processes. Return an accurate receipt.\n")
            if configured_python:
                prompt += f"Shared project interpreter (reuse without modifying it): {configured_python}\n"
            prompt += "\n" + body
            policy = permissions.policy_args(wt, project.rules, related_workspaces=(project.checkout, project.primary))
            before = time.monotonic()
            result = engine.run(wt, role=rec["role"], effort=rec["effort"], prompt=prompt, policy=policy,
                                thread=rec["thread_id"], output=attempt / "receipt.json", timeout=rec["timeout_seconds"])
            model_seconds = time.monotonic() - before
            rec.update(thread_id=result["thread_id"], status=result["status"], summary=result["summary"],
                       commands=result["commands"])
            gate_seconds = 0.0
            if result["status"] == "done":
                commit = finish_card(wt, rec, project.rules)
                if commit:
                    rec.update(commit=commit, head=commit)
                write_json(path, rec)
                if rec["head"] == rec["base"]:
                    rec["card_review"] = {"status": "no_changes", "review_status": "skipped",
                                          "test_status": "skipped", "test_commands": [],
                                          "base": rec["base"], "head": rec["head"]}
                else:
                    gate_start = time.monotonic()
                    try:
                        rec["card_review"] = card_review.run(project, rec, config, attempt, after=parent_review,
                                                             feedback=body if resume_id else None)
                        if rec["card_review"]["review_status"] == "passed":
                            rec.pop("review_parent", None)
                    except Exception as exc:
                        rec["card_review"] = {"status": "error", "base": rec["base"], "head": rec["head"],
                                              "error": str(exc)}
                    finally:
                        gate_seconds = time.monotonic() - gate_start
            elapsed = time.monotonic() - start
            rec.update(seconds=round(elapsed, 4), exec_seconds=round(model_seconds, 4),
                       gate_seconds=round(gate_seconds, 4),
                       wrapper_seconds=round(elapsed - model_seconds - gate_seconds, 4))
            rec.pop("error", None)
            return rec
        except Exception as e:
            rec.update(status="error", error=str(e))
            events = attempt / "receipt.json.events.jsonl"
            if not rec.get("thread_id") and events.is_file():
                # A first attempt can time out after Codex has opened a resumable thread.
                with events.open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            break
                        if isinstance(event, dict) and event.get("type") == "thread.started":
                            thread = event.get("thread_id")
                            if isinstance(thread, str) and thread:
                                rec["thread_id"] = thread
                            break
            raise
        finally:
            write_json(path, rec)


def cleanup(project, candidate):
    results = []
    for f in sorted((project.directory / "cards").glob("*/record.json")):
        key = f.parent.name
        try:
            with lock(project.directory / "locks" / ("card-" + key + ".lock")):
                _, card = card_record(project, key)
                if card["status"] != "done" or card.get("cleaned"):
                    continue
                wt = project.directory / "worktrees" / key
                if not ancestor(project.checkout, card["head"], candidate):
                    continue
                check_checkout(wt, card["branch"], card["head"])
                # Even ignored local caches or user files should survive a ship.
                require_clean(wt, ignored=True)
                git(project.checkout, "worktree", "remove", wt)
                card["cleaned"] = now()
                write_json(f, card)
                results.append({"id": key, "removed": True})
        except (OSError, ValueError, RuntimeError) as e:
            results.append({"id": key, "retained": str(e)})
    return results


def clean(project, ids, *, apply=False, include_ignored=False):
    """Preview or remove integrated, clean card worktrees at integration HEAD."""
    keys = [valid_id(key) for key in ids]
    if len(set(keys)) != len(keys):
        raise ValueError("provide distinct card IDs")
    if not keys:
        for f in sorted((project.directory / "cards").glob("*/record.json")):
            rec = json.loads(f.read_text(encoding="utf-8"))
            if rec.get("status") == "done" and not rec.get("cleaned"):
                keys.append(valid_id(f.parent.name))
    checkout = Path(project.checkout).resolve()
    branch = git(checkout, "symbolic-ref", "-q", "HEAD")
    if branch.startswith("refs/heads/codex-flow/card/") or checkout.is_relative_to(
            (project.directory / "worktrees").resolve()):
        raise ValueError("run clean from the integration worktree, not a card worktree")
    candidate = oid(checkout, "HEAD")
    # Check every ID before any worktree is changed.
    for key in keys:
        card_record(project, key)
    results = []
    for key in keys:
        try:
            with lock(project.directory / "locks" / ("card-" + key + ".lock")):
                path, card = card_record(project, key)
                if card.get("status") != "done" or card.get("cleaned"):
                    raise ValueError("card is not an active completed card")
                if card.get("branch") != "codex-flow/card/" + key or not card.get("head") or (
                    card.get("commit") and card["head"] != card["commit"]
                ) or (not card.get("commit") and card.get("head") != card.get("base")):
                    raise ValueError("card branch or completed commit does not match its record")
                wt = project.directory / "worktrees" / key
                if Path(card["worktree"]).resolve() != wt.resolve() or not wt.is_dir():
                    raise ValueError("card worktree does not match its record")
                if not ancestor(checkout, card["head"], candidate):
                    raise ValueError("card is not integrated into current HEAD")
                check_checkout(wt, card["branch"], card["head"])
                listing = git(checkout, "worktree", "list", "--porcelain", "-z", binary=True)
                blocks = listing.split(b"\0\0")
                worktree_entry = b"worktree " + os.fsencode(wt.resolve())
                block = next((b.split(b"\0") for b in blocks if worktree_entry in b.split(b"\0")), None)
                if block is None or any(x == b"locked" or x.startswith(b"locked ") for x in block):
                    raise ValueError("worktree is missing from Git or locked")
                if any(line.startswith(b"160000 ") for line in git(wt, "ls-files", "--stage", "-z", binary=True).split(b"\0")):
                    raise ValueError("worktree contains a submodule")
                flags = git(wt, "ls-files", "-v", "-z", binary=True).split(b"\0")
                if any(x and (x[:1].islower() or x[:1] == b"S") for x in flags):
                    raise ValueError("worktree has assume-unchanged or skip-worktree entries")
                if git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none", binary=True):
                    raise ValueError("worktree has tracked or untracked changes")
                ignored = [os.fsdecode(p) for p in git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", binary=True).split(b"\0") if p]
                row = {"id": key, "worktree": str(wt), "ignored": ignored, "removed": False,
                       "eligible": not ignored or include_ignored}
                if apply and row["eligible"]:
                    if git(checkout, "symbolic-ref", "-q", "HEAD") != branch or oid(checkout, "HEAD") != candidate:
                        raise ValueError("integration branch or HEAD changed; preview again")
                    check_checkout(wt, card["branch"], card["head"])
                    flags = git(wt, "ls-files", "-v", "-z", binary=True).split(b"\0")
                    if any(x and (x[:1].islower() or x[:1] == b"S") for x in flags):
                        raise ValueError("index visibility changed; preview again")
                    if git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none", binary=True):
                        raise ValueError("worktree changed; preview again")
                    if not include_ignored and git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", binary=True):
                        raise ValueError("ignored files appeared; preview again")
                    git(checkout, "worktree", "remove", wt)
                    card["cleaned"] = now()
                    write_json(path, card)
                    row["removed"] = True
                results.append(row)
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            results.append({"id": key, "removed": False, "retained": str(exc)})
    return results


def release(project, base_ref, candidate_ref, *, deliver, after=None, require_incremental=False):
    with lock(project.directory / "locks" / "ship.lock"):
        config = project.config()
        base_ref = base_ref or config["base"]
        base, candidate = oid(project.checkout, base_ref), oid(project.checkout, candidate_ref)
        if oid(project.checkout, "HEAD") != candidate:
            raise ValueError("check out the exact candidate at HEAD before review or ship")
        require_clean(project.checkout)
        # Git sends all new commits when publishing. Check changed names even if
        # a later commit removed one of those paths from the final tree.
        changed = git(project.checkout, "log", "-m", "--format=", "--no-renames", "--name-only",
                      "-z", "--diff-filter=AMT", f"{base}..{candidate}", "--", binary=True)
        protect_changed_names(project.checkout, changed, permissions.credential_matcher(project.checkout, project.rules))
        assessment = coverage.assess(project, config, base, candidate)
        # Only stable card evidence belongs in the context. A subsequent fix
        # can add unreviewed commits while reusing the original review thread.
        review_context = {"reviewed_cards": assessment["reviewed_cards"]}
        folder = reviews.evidence_folder(project, base, candidate)
        folder.mkdir(parents=True, exist_ok=True)
        verdict = reviews.run_review(project, config, base, candidate, folder, after=after,
                                     require_incremental=require_incremental,
                                     review_context=review_context, instructions=coverage.instructions(assessment))
        verdict = {**verdict, "coverage": assessment}
        if not deliver or verdict["status"] != "passed":
            return verdict
        if (oid(project.checkout, base_ref) != base or oid(project.checkout, candidate_ref) != candidate
                or oid(project.checkout, "HEAD") != candidate):
            raise ValueError("base or candidate moved; restart ship")
        current_coverage = coverage.assess(project, project.config(), base, candidate)
        if current_coverage["reviewed_cards"] != assessment["reviewed_cards"]:
            raise ValueError("card review evidence changed during integration review; restart ship")
        require_clean(project.checkout)
        sites = config.get("sites", [])
        result = {"status": "passed", "base": base, "candidate": candidate,
                  "review": verdict["report"], "coverage": assessment}
        if sites:
            record = project.directory / "deployments" / (uuid.uuid4().hex + ".json")
            try:
                deployed = delivery.deploy(project.checkout, base, candidate, sites, record)
            except (OSError, ValueError, RuntimeError) as e:
                raise RuntimeError(f"{e}; deployment record: {record}") from e
            result.update(status="deployed", deployment=str(record))
            result["sites"] = [
                {"name": site["name"], "verified_head": candidate,
                 "tests": [step["step"] for step in site["steps"] if step["step"].startswith("test_")],
                 "published": any(step["step"] == "publish" for step in site["steps"])}
                for site in deployed["sites"]
            ]
        result["cleanup"] = cleanup(project, candidate)
        return result


def status(project, key):
    if key:
        return card_record(project, key)[1]
    initialized = project.settings.is_file()
    return {"project_config": str(project.settings), "state": str(project.directory), "drafts": str(project.drafts),
            "initialized": initialized, "base": project.config()["base"] if initialized else None,
            "cards": [{**{k: row.get(k) for k in ("id", "status", "head", "cleaned")},
                       "card_review_status": (row.get("card_review") or {}).get("status")}
                      for row in (json.loads(p.read_text(encoding="utf-8"))
                                  for p in sorted((project.directory / "cards").glob("*/record.json")))],
            "deployments": [p.stem for p in sorted((project.directory / "deployments").glob("*.json"))]}
