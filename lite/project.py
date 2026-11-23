"""Repository identity, on-disk records and card metadata."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib

DEFAULT_RULES = Path(__file__).resolve().parents[1] / "defaults" / "credentials.toml"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(where, *args, binary=False, check=True):
    p = subprocess.run(["git", "-C", str(where), *map(str, args)], capture_output=True)
    if check and p.returncode:
        detail = p.stderr.decode(errors="replace").strip() or f"exit {p.returncode}"
        raise RuntimeError(f"git {args[0]}: {detail}")
    if not check:
        return p
    return p.stdout if binary else os.fsdecode(p.stdout).rstrip("\n")


def oid(repo, ref):
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


def ancestor(repo, older, newer):
    rc = git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode
    if rc > 1:
        raise RuntimeError("Git could not compare commit ancestry")
    return rc == 0


def require_clean(repo, *, ignored=False):
    options = ["--ignored=matching"] if ignored else []
    if git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", *options, binary=True):
        raise ValueError(f"worktree has outstanding files: {repo}")


def valid_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("ID must start with a letter or digit and contain only letters, digits, - and _ (max 63)")
    return value


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as f:
            name = f.name
            json.dump(data, f, ensure_ascii=True, indent=2)
            f.write("\n")
        os.replace(name, path)
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"already running: {path.stem}") from None
        yield


def roots():
    home = Path.home()
    config = Path(os.environ.get("CODEX_FLOW_CONFIG_HOME") or
                  Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "codex-flow-lite")
    state = Path(os.environ.get("CODEX_FLOW_STATE_HOME") or
                 Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / "codex-flow-lite")
    return config.expanduser().resolve(), state.expanduser().resolve()


@dataclass(frozen=True)
class Project:
    checkout: Path
    primary: Path
    common: Path
    settings: Path
    directory: Path
    rules: Path
    state_root: Path

    @property
    def drafts(self):
        return self.directory / "drafts"

    @classmethod
    def find(cls, cwd):
        repo = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
        common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        config, state = roots()
        # Also check the primary checkout when the caller is itself in a linked worktree.
        if common.name == ".git":
            primary = common.parent
        else:
            first = git(repo, "worktree", "list", "--porcelain", "-z", binary=True).split(b"\0", 1)[0]
            if not first.startswith(b"worktree "):
                raise ValueError("cannot determine the primary Git checkout")
            primary = Path(os.fsdecode(first[len(b"worktree "):])).resolve()
        if any(p.is_relative_to(r) for p in (state, config) for r in (repo, primary)):
            raise ValueError("config and state must be outside the repository")
        key = hashlib.sha256(os.fsencode(common)).hexdigest()[:20]
        return cls(repo, primary, common, config / "projects" / (key + ".toml"),
                   state / "projects" / key, config / "credentials.toml", state)

    def config(self):
        if not self.settings.is_file():
            raise ValueError(f"initialize the project first: codex-flow init --base <ref> ({self.settings})")
        with self.settings.open("rb") as f:
            config = tomllib.load(f)
        self.validate_config(config)
        return config

    @staticmethod
    def validate_config(config):
        if set(config) - {"base", "sites", "interpreter", "review_focus", "timeout_seconds", "card_tests", "worktree"}:
            raise ValueError("unknown project field; see examples/project.toml")
        from . import worktree
        worktree.validate(config)
        if not isinstance(config.get("base"), str) or not config["base"].strip():
            raise ValueError("project configuration requires a Git base ref")
        if not isinstance(config.get("sites", []), list):
            raise ValueError("sites must be an array")
        if not isinstance(config.get("review_focus", ""), str):
            raise ValueError("review_focus must be text")
        if "interpreter" in config and not isinstance(config["interpreter"], str):
            raise ValueError("interpreter must name an absolute local executable")
        if "card_tests" in config and (
            not isinstance(config["card_tests"], list)
            or any(not isinstance(command, str) or not command.strip() for command in config["card_tests"])
        ):
            raise ValueError("card_tests must be an array of nonempty command strings")
        timeout = config.get("timeout_seconds", 7200)
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive integer")

    def initialize(self, base, config_file):
        text = (Path(config_file).read_text(encoding="utf-8") if config_file
                else "base = " + json.dumps(base or "HEAD", ensure_ascii=False) + "\nsites = []\n")
        data = tomllib.loads(text)
        self.validate_config(data)
        ref = data["base"]
        # HEAD is an input shorthand. Freeze a named target, never the changing integration HEAD.
        if ref == "HEAD":
            if config_file:
                raise ValueError("use a named base ref or a full commit in the TOML")
            ref = git(self.checkout, "symbolic-ref", "-q", "HEAD")
            text = "base = " + json.dumps(ref) + "\nsites = []\n"
        oid(self.checkout, ref)
        self.state_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.drafts.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.rules.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            with self.rules.open("x", encoding="utf-8") as f:
                f.write(DEFAULT_RULES.read_text(encoding="utf-8"))
        except FileExistsError:
            pass
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.settings.open("x", encoding="utf-8") as f:
                f.write(text)
        except FileExistsError:
            raise ValueError(f"already initialized; edit {self.settings} to change settings") from None
        return {"config": str(self.settings), "state": str(self.directory), "rules": str(self.rules),
                "drafts": str(self.drafts), "base": ref}


def read_card(path, *, existing_id=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError("expected a task card of at most 128 KiB")
    body = path.read_text(encoding="utf-8")
    meta = {}
    lines = body.splitlines()
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            raise ValueError("unterminated card frontmatter") from None
        for line in lines[1:end]:
            if not line.strip() or line.startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if not sep or key not in ("id", "effort") or key in meta:
                raise ValueError(f"invalid or duplicate card field: {key}")
            meta[key] = value.strip()
        body = "\n".join(lines[end + 1:])
    if not body.strip():
        raise ValueError("empty task card")
    if "effort" in meta and meta["effort"] not in EFFORTS:
        raise ValueError("unknown reasoning effort")
    if existing_id is not None and meta.get("id", existing_id) != existing_id:
        raise ValueError("feedback names a different card")
    return valid_id(meta.get("id", existing_id or path.stem)), meta.get("effort"), body
