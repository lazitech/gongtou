"""Command line interface. Structured stdout is suitable for Claude or a terminal."""
import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from . import deploy, probe, workflow
from .project import DEFAULT_RULES, Project, oid, roots


def parser():
    cli = argparse.ArgumentParser(prog="codex-flow", description="Parallel worktrees and reviewed Git delivery")
    cli.add_argument("-C", "--repo", default=".", help="local checkout or integration worktree")
    sub = cli.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create the external project config and editable credential list")
    opts = init.add_mutually_exclusive_group()
    opts.add_argument("--base", help="base ref, e.g. refs/remotes/srv/main")
    opts.add_argument("--config", type=Path, help="copy project TOML into the user's external config directory")
    validate = sub.add_parser("validate-config", help="validate project TOML locally without contacting sites")
    validate.add_argument("path", nargs="?", type=Path, help="candidate TOML (default: installed project config)")
    task = sub.add_parser("task", help="start a card in a new worktree")
    task.add_argument("role", choices=("sol", "astra"))
    task.add_argument("card", type=Path)
    task.add_argument("--base", help="override the configured base, e.g. for a fix card")
    resume = sub.add_parser("resume", help="continue a card in its original thread and worktree")
    resume.add_argument("id")
    resume.add_argument("feedback", type=Path)
    status = sub.add_parser("status", help="list runs or print a card's record and observed commands")
    status.add_argument("id", nargs="?")
    clean = sub.add_parser("clean", help="preview removal of completed, integrated card worktrees")
    clean.add_argument("ids", nargs="*")
    clean.add_argument("--apply", action="store_true", help="perform the displayed kind of cleanup")
    clean.add_argument("--include-ignored", action="store_true", help="also select worktrees containing ignored files")
    sub.add_parser("selfcheck", help="repeat the local synthetic permission probe")
    for name in ("review", "ship"):
        cmd = sub.add_parser(name, help=("review the complete commit range" if name == "review" else
                                        "review, then deliver through every configured site and clean up"))
        cmd.add_argument("--base", help="base ref or full OID (default: project base)")
        cmd.add_argument("--after-review", type=Path, help="prior full needs_changes report for one follow-up")
        cmd.add_argument("--require-incremental", action="store_true", help="stop if the specified thread cannot resume")
        cmd.add_argument("--candidate", default="HEAD", help="integrated candidate ref or commit")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "selfcheck":
            config, state = roots()
            rules = config / "credentials.toml"
            result = probe.ensure_probe(rules if rules.exists() else DEFAULT_RULES, state, force=True)
        else:
            project = Project.find(args.repo)
            if args.command == "init":
                result = project.initialize(args.base, args.config)
            elif args.command == "validate-config":
                path = args.path if args.path is not None else project.settings
                with path.open("rb") as stream:
                    config = tomllib.load(stream)
                project.validate_config(config)
                deploy.validate_sites(config.get("sites", []))
                base = oid(project.checkout, config["base"])
                interpreter = config.get("interpreter")
                if interpreter is not None:
                    executable = Path(interpreter)
                    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
                        raise ValueError("configured interpreter must be an absolute, executable local path")
                result = {"valid": True, "config": str(path.resolve()), "base_oid": base}
            elif args.command == "task":
                result = workflow.task(project, args.role, args.card, base=args.base)
            elif args.command == "resume":
                result = workflow.task(project, None, args.feedback, resume_id=args.id)
            elif args.command == "status":
                result = workflow.status(project, args.id)
            elif args.command == "clean":
                result = {"worktrees": workflow.clean(project, args.ids, apply=args.apply,
                                                       include_ignored=args.include_ignored)}
            else:
                if args.require_incremental and not args.after_review:
                    raise ValueError("--require-incremental requires --after-review")
                result = workflow.release(project, args.base, args.candidate, deliver=args.command == "ship",
                                          after=args.after_review, require_incremental=args.require_incremental)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"codex-flow: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if args.command == "status":
        return 0
    if args.command in ("task", "resume") and result.get("status") == "done":
        gate = result.get("card_review", {}).get("status")
        return {"passed": 0, "no_changes": 0, "needs_changes": 2, "tests_failed": 5, "tests_unverified": 6}.get(gate, 1)
    return {"needs_changes": 2, "partial": 3, "blocked": 4}.get(result.get("status"), 0)
