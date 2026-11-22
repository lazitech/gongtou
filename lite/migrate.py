"""Register lite skills and optionally remove the exact R6 integration."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile


R6_SKILL_SHA256 = "f60d3f01a7f22d4a018dc93e60c9e022e3a825c34345311b4a1453ca07fbb187"
R6_BACKUP_NAME = re.compile(r"SKILL\.md\.bak-[0-9]{8}-[0-9]{6}-[1-9][0-9]*")


def register(claude, dest, migrate):
    claude, dest = map(Path, (claude, dest))
    skills = claude / "skills"
    settings = (claude / "settings.json").resolve() if migrate else claude / "settings.json"
    settings_data = None
    old = skills / "codex-enable"
    remove_old = False
    if migrate:
        if old.exists() or old.is_symlink():
            if old.is_symlink() or not (old / "SKILL.md").is_file():
                raise ValueError("cannot identify existing codex-enable as R6")
            if hashlib.sha256((old / "SKILL.md").read_bytes()).hexdigest() != R6_SKILL_SHA256:
                raise ValueError("existing codex-enable differs from known R6; left unchanged")
            for entry in old.iterdir():
                if entry.name == "SKILL.md":
                    continue
                if not R6_BACKUP_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_file():
                    raise ValueError("codex-enable contains additional files; left unchanged")
            remove_old = True
        if settings.exists():
            original_settings = settings.read_bytes()
            settings_data = json.loads(original_settings)
            if not isinstance(settings_data, dict) or not isinstance(settings_data.get("hooks", {}), dict):
                raise ValueError("invalid Claude settings")
            hooks = settings_data.get("hooks", {})
            changed = False
            for event, suffix in (("SessionStart", "hook-session-start"), ("Stop", "hook-stop")):
                if event not in hooks:
                    continue
                if not isinstance(hooks[event], list):
                    raise ValueError("invalid hook list")
                kept = []
                for group in hooks[event]:
                    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                        raise ValueError("invalid hook group")
                    commands = [h for h in group["hooks"] if not (isinstance(h, dict)
                        and h.get("type") == "command" and h.get("command") ==
                        "~/.claude/codex-workflow/bin/codex-flow " + suffix)]
                    changed |= len(commands) != len(group["hooks"])
                    if commands or not group["hooks"]:
                        kept.append({**group, "hooks": commands})
                hooks[event] = kept
            if not changed:
                settings_data = None
    skills.mkdir(parents=True, exist_ok=True)
    targets = [(skills / "gongtou", dest / "skills" / "gongtou")]
    owned_old = []
    for name in ("ship", "codex-task"):
        link = skills / name
        expected = dest / "skills" / name
        if link.is_symlink() and os.path.abspath(link.parent / os.readlink(link)) == os.path.abspath(expected):
            owned_old.append((link, expected))
        elif link.exists() or link.is_symlink():
            print(f"Leaving user skill unchanged: {link}")
    changes = [(target, source) for target, source in targets
               if not (target.is_symlink() and os.readlink(target) == str(source))]
    if not changes and not owned_old and not remove_old and settings_data is None:
        return
    backup_root = claude / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="codex-flow-lite-", dir=backup_root))
    moved, created = [], []
    settings_backup = None
    try:
        if settings_data is not None:
            settings_backup = backup / "settings.json"
            shutil.copy2(settings, settings_backup)
        for target, source in changes:
            if target.exists() or target.is_symlink():
                saved = backup / target.name
                shutil.move(str(target), str(saved)); moved.append((target, saved))
            target.symlink_to(source, target_is_directory=True); created.append(target)
        for link, source in owned_old:
            saved = backup / link.name
            shutil.move(str(link), str(saved)); moved.append((link, saved))
            if source.exists() or source.is_symlink():
                sources = backup / "installed-skills"
                sources.mkdir(exist_ok=True)
                saved_source = sources / source.name
                shutil.move(str(source), str(saved_source))
                moved.append((source, saved_source))
        if remove_old:
            shutil.move(str(old), str(backup / "codex-enable")); moved.append((old, backup / "codex-enable"))
        if settings_data is not None:
            fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=settings.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(settings_data, f, ensure_ascii=False, indent=2); f.write("\n")
                os.chmod(tmp, settings.stat().st_mode & 0o777)
                if settings.read_bytes() != original_settings:
                    raise ValueError("Claude settings changed during registration; retry migration")
                os.replace(tmp, settings)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
    except Exception:
        for target in reversed(created): target.unlink(missing_ok=True)
        for target, saved in reversed(moved): shutil.move(str(saved), str(target))
        raise
    print("Previous registrations/settings backed up at", backup)


if __name__ == "__main__":
    try:
        register(sys.argv[1], sys.argv[2], sys.argv[3] == "1")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("skill registration failed:", exc, file=sys.stderr)
        sys.exit(1)
