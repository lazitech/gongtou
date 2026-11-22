"""Describe which integrated card ranges have current review and test evidence."""
import json
from . import card_review, reviews
from .project import ancestor, git


def assess(project, config, base, candidate):
    """Return stable reviewed evidence and a candidate-specific commit summary.

    This is guidance for Sol, not a claim that merge results have been reviewed.
    Each card's entire recorded range was reviewed before integration. Commits
    outside those ranges and every merge resolution remain in scope.
    """
    release_commits = set(git(project.checkout, "rev-list", f"{base}..{candidate}").splitlines())
    covered = set()
    reviewed, unreviewed = [], []
    for path in sorted((project.directory / "cards").glob("*/record.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
            head, card_base = card.get("head"), card.get("base")
            if (not isinstance(head, str) or not isinstance(card_base, str)
                    or not reviews.OID.fullmatch(head) or not reviews.OID.fullmatch(card_base)
                    or not card.get("commit") or card.get("id") != path.parent.name):
                continue
            # A card whose last recorded tip is not in this candidate does not
            # supply evidence for its contents (e.g. a cherry-pick or squash).
            if not ancestor(project.checkout, head, candidate) or ancestor(project.checkout, head, base):
                continue
            if not ancestor(project.checkout, card_base, head):
                continue
            check = card_review.is_current(project, config, card)
            if check is not True:
                reason = check if isinstance(check, str) else "card review or tests lack current passing evidence"
                unreviewed.append({"id": card["id"], "head": head, "reason": reason})
                continue
            gate = card["card_review"]
            reviewed.append({"id": card["id"], "base": card_base, "head": head,
                             "report": gate["report"], "acceptance_sha256": gate["acceptance_sha256"]})
            covered.update(git(project.checkout, "rev-list", f"{card_base}..{head}").splitlines())
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            # Corrupt, missing, or stale evidence never narrows review scope.
            unreviewed.append({"id": path.parent.name, "reason": "cannot verify card evidence"})
    outside = sorted(release_commits - covered)
    return {"reviewed_cards": reviewed, "unreviewed_cards": unreviewed,
            "unreviewed_commit_count": len(outside), "unreviewed_commits_sample": outside[:20]}


def instructions(summary):
    reviewed = json.dumps(summary["reviewed_cards"], ensure_ascii=True, sort_keys=True)
    unreviewed = json.dumps(summary["unreviewed_cards"], ensure_ascii=True, sort_keys=True)
    outside = json.dumps(summary["unreviewed_commits_sample"], ensure_ascii=True)
    return ("Integration review: check every merge's conflict resolution, changes authored by the integrator, "
            "and interactions across cards and affected callers. Inspect the full base-to-candidate diff when useful. "
            "The following exact card ranges have independently passed their card review and tests; "
            "use that context to focus, but check their integration and any changes made while merging. "
            f"Reviewed card evidence: {reviewed}\n"
            f"Cards whose evidence is absent or stale require full review: {unreviewed}\n"
            f"Other commits outside reviewed card ranges total {summary['unreviewed_commit_count']}; "
            f"sample: {outside}. Fully review all changes outside the reviewed ranges.")
