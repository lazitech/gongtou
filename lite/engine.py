"""Non-interactive Codex execution and strict receipt validation."""
import json
import os
from pathlib import Path
import signal
import subprocess

MODELS = {"sol": "gpt-6.1-sol", "astra": "gpt-6.2-astra", "review": "gpt-6.1-sol"}
FINDING = {"type": "object", "additionalProperties": False,
           "required": ["claim", "file", "line"], "properties": {
               "claim": {"type": "string"}, "file": {"type": "string"}, "line": {"type": "integer"}}}


class SessionUnavailable(RuntimeError):
    """Codex positively reported that the requested thread has no rollout."""


def schema(role, prior_ids=None, report_tests=False):
    review = role == "review"
    props = {"status": {"type": "string", "enum": ["passed", "needs_changes"] if review else ["done", "partial", "blocked"]},
             "summary": {"type": "string"}}
    if review:
        props["findings"] = {"type": "array", "items": FINDING}
        if report_tests:
            props["test_commands"] = {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["command", "exit_code"],
                "properties": {"command": {"type": "string"}, "exit_code": {"type": "integer"}}}}
        if prior_ids is not None:
            props["resolutions"] = {
                "type": "array",
                "items": {"type": "object", "additionalProperties": False,
                          "required": ["id", "status", "reason"],
                          "properties": {"id": {"type": "string", "enum": list(prior_ids)},
                                         "status": {"type": "string", "enum": ["resolved", "still_open"]},
                                         "reason": {"type": "string"}}}}
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


def _events(path):
    """Read JSONL one frame at a time and retain only the execution summary."""
    terminal, ids, commands = [], [], []
    with path.open(encoding="utf-8") as source:
        for line in source:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("malformed event")
            kind = event.get("type")
            if kind in ("turn.started", "turn.completed", "turn.failed", "error"):
                terminal.append(kind)
            if kind == "thread.started":
                ids.append(event.get("thread_id"))
            item = event.get("item", {})
            if kind == "item.completed" and isinstance(item, dict) and item.get("type") == "command_execution":
                command, code = item.get("command"), item.get("exit_code")
                if not isinstance(command, str) or not (code is None or type(code) is int):
                    raise ValueError("invalid command event")
                commands.append({"command": command, "exit_code": code})
    return terminal, ids, commands


def run(worktree: Path, *, role: str, effort: str, prompt: str, policy: list[str], output: Path,
        thread: str | None = None, env: dict | None = None, timeout: float = 7200,
        prior_ids: list[str] | None = None, report_tests: bool = False) -> dict:
    return _run(worktree, role=role, effort=effort, prompt=prompt, policy=policy, output=output, thread=thread, env=env, timeout=timeout, prior_ids=prior_ids, report_tests=report_tests)


def _execute(args, *, prompt, stdout, stderr, cwd, env, timeout):
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                          cwd=cwd, env=env, text=True, start_new_session=True) as process:
        try:
            process.communicate(prompt, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        return process.returncode


def _run(worktree: Path, *, role: str, effort: str, prompt: str, policy: list[str], output: Path,
         thread: str | None = None, env: dict | None = None, timeout: float | None = None,
         prior_ids: list[str] | None = None, report_tests: bool = False) -> dict:
    if role not in MODELS or effort not in ("low", "medium", "high", "xhigh", "max", "ultra"):
        raise ValueError("invalid role or reasoning effort")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float('inf')):
        raise ValueError("timeout must be a positive finite number of seconds")
    if any(x in ("-s", "--sandbox") or x.startswith("--sandbox=") for x in policy):
        raise ValueError("sandbox flag overrides permission policy")
    if prior_ids is not None:
        if role != "review" or not thread or not isinstance(prior_ids, list) or not prior_ids or any(
                not isinstance(value, str) or not value for value in prior_ids) or len(set(prior_ids)) != len(prior_ids):
            raise ValueError("incremental review requires a thread and distinct finding IDs")
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    schema_path = output.with_name(output.name + ".schema.json")
    events_path = output.with_name(output.name + ".events.jsonl")
    errors_path = output.with_name(output.name + ".stderr.log")
    schema_path.write_text(json.dumps(schema(role, prior_ids, report_tests)), encoding="utf-8")
    # Never accept a receipt left by a previous invocation.
    output.unlink(missing_ok=True)
    args = ["codex", "exec", "--strict-config", "-C", str(worktree), "-m", MODELS[role],
            "-c", f'model_reasoning_effort="{effort}"', *policy]
    if thread:
        args += ["resume", "-m", MODELS[role], "-c", f'model_reasoning_effort="{effort}"', *policy, thread]
    args += ["--json", "--output-schema", str(schema_path), "-o", str(output), "-"]
    try:
        with events_path.open("w", encoding="utf-8") as events, errors_path.open("w", encoding="utf-8") as errors:
            returncode = _execute(args, prompt=prompt, stdout=events, stderr=errors,
                                  cwd=worktree, env=env, timeout=timeout)
        terminal, ids, commands = _events(events_path)
        thread_id = ids[0] if ids else thread
        if len(ids) > 1 or (thread and ids and ids[0] != thread):
            raise ValueError('session id mismatch')
        if returncode:
            if thread and events_path.stat().st_size == 0:
                error = errors_path.read_text(encoding="utf-8", errors="replace").strip()
                missing = (f"Error: thread/resume: thread/resume failed: no rollout found for thread id "
                           f"{thread} (code -32600)")
                if error == missing:
                    raise SessionUnavailable(f"Codex session {thread} is unavailable")
            if timeout is not None and timeout <= 30:
                with errors_path.open('rb') as errors:
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 1000))
                    detail = errors.read().decode('utf-8', errors='replace')
            else:
                detail = str(errors_path)
            raise ValueError(f"Codex exited {returncode}; stderr: {detail}")
        data = json.loads(output.read_text(encoding="utf-8"))
        expected = schema(role, prior_ids, report_tests)
        if terminal != ['turn.started', 'turn.completed'] or not isinstance(thread_id, str) or not thread_id:
            raise ValueError("incomplete Codex transport")
        if not isinstance(data, dict) or set(data) != set(expected["required"]) or data["status"] not in expected["properties"]["status"]["enum"] or not isinstance(data["summary"], str):
            raise ValueError("invalid receipt")
        if role == "review":
            if not isinstance(data["findings"], list) or any(not isinstance(f, dict) or set(f) != {"claim", "file", "line"} or not isinstance(f["claim"], str) or not isinstance(f["file"], str) or type(f["line"]) is not int for f in data["findings"]):
                raise ValueError("invalid findings")
            if report_tests:
                claimed = data["test_commands"]
                if not isinstance(claimed, list) or any(
                    not isinstance(c, dict) or set(c) != {"command", "exit_code"} or
                    not isinstance(c["command"], str) or type(c["exit_code"]) is not int for c in claimed
                ):
                    raise ValueError("invalid card test command receipt")
            resolved = True
            if prior_ids is not None:
                resolutions = data["resolutions"]
                if not isinstance(resolutions, list) or any(
                        not isinstance(item, dict) or set(item) != {"id", "status", "reason"}
                        or not isinstance(item["id"], str) or not isinstance(item["reason"], str)
                        or item["status"] not in ("resolved", "still_open") for item in resolutions):
                    raise ValueError("invalid resolution")
                ids = [item["id"] for item in resolutions]
                if len(ids) != len(prior_ids) or set(ids) != set(prior_ids):
                    raise ValueError("each previous finding must be answered exactly once")
                resolved = all(item["status"] == "resolved" for item in resolutions)
            if (data["status"] == "passed") != (len(data["findings"]) == 0 and resolved):
                raise ValueError("inconsistent review conclusion")
        return {**data, "thread_id": thread_id, "commands": commands}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError, KeyError) as exc:
        raise RuntimeError(f"Codex execution failed; artifacts retained at {output.parent}: {exc}") from exc
