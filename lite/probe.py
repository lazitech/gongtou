"""Fail-closed, local-only execution probe for the installed Codex sandbox."""
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import tomllib

from .engine import _run
from .permissions import policy_args


def ensure_probe(rules: Path, state: Path, *, force=False) -> dict:
    rules, state = Path(rules).resolve(), Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'probe.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = state / 'probe.json'
        try:
            version = subprocess.run(['codex', '--version'], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
            from . import permissions
            key = hashlib.sha256(version.encode() + b'\0' + rules.read_bytes() + b'\0' + Path(permissions.__file__).read_bytes()
                                 + b'\0' + Path(__file__).read_bytes() + b'\0' + Path(__file__).with_name('engine.py').read_bytes()).hexdigest()
        except Exception as exc:
            record.unlink(missing_ok=True)
            raise RuntimeError(f'Cannot validate probe configuration: {exc}') from exc
        if not force and record.exists():
            try:
                cached = json.loads(record.read_text())
                if cached.get('ok') is True and cached.get('key') == key:
                    return cached
            except (ValueError, OSError, AttributeError):
                pass
        record.unlink(missing_ok=True)
        try:
            _probe(rules)
        except Exception as exc:
            raise RuntimeError(f'Codex permission probe failed (temporary fixtures were removed): {exc}') from exc
        result = {'ok': True, 'key': key, 'codex_version': version}
        temp = state / 'probe.json.tmp'
        temp.write_text(json.dumps(result))
        os.replace(temp, record)
        return result


def _probe(rules):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            outputs = [item for item in body.get('input', []) if item.get('type') == 'function_call_output']
            calls.append(outputs)
            if not outputs:
                item = {'type': 'function_call', 'id': 'fc_probe', 'call_id': 'call_probe', 'name': 'exec_command',
                        'arguments': json.dumps({'cmd': "python3 -c 'import errno; print(\"CONTROL=\" + open(\"control.txt\").read());\ntry: open(\"probe-denied.txt\").read(); print(\"DENIED=UNEXPECTED\")\nexcept OSError as e: print(\"DENIED=\" + errno.errorcode.get(e.errno, str(e.errno)))'"})}
            else:
                item = {'type': 'message', 'id': 'msg_probe', 'role': 'assistant', 'content': [{'type': 'output_text',
                    'text': json.dumps({'status': 'done', 'summary': 'probe'})}]}
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            for event in ({'type': 'response.created', 'response': {'id': 'resp_probe'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': 'resp_probe', 'usage': {'input_tokens': 1, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 1, 'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 2}}}):
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()); self.wfile.flush()
    with tempfile.TemporaryDirectory(prefix='codex-lite-probe-') as tmp:
        base = Path(tmp); work = base / 'repo'; work.mkdir(); home = base / 'home'; home.mkdir(); codex = base / 'codex'; codex.mkdir()
        for parent in ('.ssh', '.aws', '.gnupg', '.azure', '.kube', '.mozilla', '.config/gh', '.config/gcloud', '.config/google-chrome', '.config/chromium', '.config/keyring', '.docker', '.claude', '.cache/huggingface', '.huggingface', '.local/share/keyrings', '.zeron/agent-accounts', '.zeron/browser'):
            (home / parent).mkdir(parents=True, exist_ok=True)
        (work / 'control.txt').write_text('probe-control')
        (work / 'probe-denied.txt').write_text('probe-forbidden')
        supplemental = tomllib.loads(rules.read_text(encoding='utf-8'))
        if set(supplemental) != {'deny'} or not isinstance(supplemental['deny'], list):
            raise ValueError('credential rules must contain a deny array')
        supplemental['deny'].append('{workspace}/probe-denied.txt')
        probe_rules = base / 'rules.toml'
        probe_rules.write_text('deny = ' + json.dumps(supplemental['deny'], ensure_ascii=False) + '\n', encoding='utf-8')
        before = ((work / 'control.txt').read_bytes(), (work / 'probe-denied.txt').read_bytes())
        subprocess.run(['git', 'init', '-q', '--template=', str(work)], check=True, env={'PATH': os.defpath, 'HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1'})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(home), 'CODEX_HOME': str(codex),
               'OPENAI_API_KEY': 'synthetic', 'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
               'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1',
               'http_proxy': 'http://127.0.0.1:1', 'https_proxy': 'http://127.0.0.1:1', 'all_proxy': 'http://127.0.0.1:1',
               'GIT_CONFIG_NOSYSTEM': '1'}
        args = policy_args(work, probe_rules, env=env) + ['-c', 'model_provider="probe"', '-c',
            f'model_providers.probe={{name="probe",base_url="http://127.0.0.1:{server.server_port}/v1",wire_api="responses",env_key="OPENAI_API_KEY",request_max_retries=0,stream_max_retries=0}}']
        try:
            _run(work, role='sol', effort='low', prompt='Run the diagnostic command and return the receipt.', policy=args, output=base / 'receipt.json', env=env, timeout=30)
        finally:
            server.shutdown(); server.server_close(); thread.join()
        events = [json.loads(line) for line in (base / 'receipt.json.events.jsonl').read_text().splitlines()]
        commands = [e.get('item', {}) for e in events if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'command_execution']
        if len(calls) != 2 or len(calls[-1]) != 1 or calls[-1][0].get('call_id') != 'call_probe' or len(commands) != 1:
            raise RuntimeError('probe command did not execute exactly once')
        text = commands[0].get('aggregated_output', '')
        observed = json.dumps(calls[-1])
        denied = 'DENIED=EACCES' if 'DENIED=EACCES' in text else 'DENIED=EPERM'
        if (commands[0].get('exit_code') != 0 or 'CONTROL=probe-control' not in text or denied not in text
                or 'probe-forbidden' in text or 'CONTROL=probe-control' not in observed
                or denied not in observed or 'probe-forbidden' in observed
                or before != ((work / 'control.txt').read_bytes(), (work / 'probe-denied.txt').read_bytes())):
            raise RuntimeError('control or denied-read result did not match')
