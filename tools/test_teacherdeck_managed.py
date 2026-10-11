# SPDX-License-Identifier: MIT
"""Check the pinned private entrypoint without any Gentoo request."""
import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def test(runtime, source):
    def inventory():
        return {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in runtime.rglob('*') if p.is_file()}
    before = inventory()
    requests = b'{"id":"bad","method":"not-allowed","args":{}}\n' \
               b'{"id":"fields","method":"connect","args":{"selector":"forbidden"}}\n'
    with tempfile.TemporaryDirectory() as directory:
        run = subprocess.run([str(runtime/'python.exe'), '-I', '-S', '-B', str(source/'teacherdeck_worker.py'),
                              '--data-dir', directory], input=requests, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=20,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if run.returncode != 0 or run.stderr:
        raise ValueError('entrypoint_failed')
    replies = [json.loads(line) for line in run.stdout.splitlines()]
    if [r.get('error', {}).get('code') for r in replies] != ['INVALID_METHOD', 'INVALID_ARGS']:
        raise ValueError('protocol_mismatch')
    if inventory() != before:
        raise ValueError('runtime_mutated')
    return {'passed': True, 'requests': 2, 'runtime_unchanged': True, 'actual_messenger_operated': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = test(args.runtime.resolve(), args.source.resolve())
    except Exception:
        result = {'passed': False, 'code': 'MANAGED_PROTOCOL_FAILED', 'values_omitted': True}
    print(json.dumps(result))
    return int(not result['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
