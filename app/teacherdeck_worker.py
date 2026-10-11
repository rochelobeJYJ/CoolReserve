# SPDX-License-Identifier: MIT
"""Fixed private JSON-line adapter for TeacherDeck; no HTTP or Google client.

Only the trusted host launches this entrypoint and supplies its app-owned ledger
directory. Public modules never see selectors, account IDs, attachment paths,
provider diagnostics or SQLite. All scheduling and attempt state reuse the
original Store/Runner; this adapter does not implement a second runner.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from datetime import timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True

from .core import Store, LOCKED, clean_job, now, signature, validate_job
from .engine import Runner
from .gentoo import initialize_profile, read_gentoo_login_marker, build_profile, bind_gentoo_self, gentoo_self_expected
from .recipients import equivalent_attempt
from .verification import available as verification_available
from .windows import WindowsDriver, automation_runtime, com_session, recipient_identity, profile_errors

MAX_LINE = 512 * 1024
MAX_TEXT = 256 * 1024
METHODS = frozenset(('connect', 'recipients', 'plan', 'start', 'status', 'cancel'))
STAGES = frozenset(('idle', 'validating', 'reading', 'preparing', 'verifying',
                    'submitting', 'checking_result', 'done', 'stopped', 'failed', 'needs_review'))
STATES = {'draft': 'draft', 'preparing': 'preparing', 'prepared': 'prepared',
          'submitting': 'submitting', 'needs_review': 'registration-requested',
          'confirmed': 'confirmed', 'uncertain': 'uncertain', 'failed': 'failed'}


class PrivateError(Exception):
    """Fixed machine-readable failure. Never contain user input or diagnostics."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def exact(args, required, optional=()):
    if (not isinstance(args, dict) or not set(required).issubset(args)
            or set(args) - set(required) - set(optional)):
        raise PrivateError('INVALID_ARGS')


def token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160:
        raise PrivateError('INVALID_ARGS')
    return value


class GentooProvider:
    """Use only the original fixed provider, with fresh login evidence."""
    def connect(self, previous):
        profile = initialize_profile(previous)['profile']
        with com_session():
            marker = read_gentoo_login_marker(profile)
            binding = profile.get('self_binding')
            if isinstance(binding, dict) and binding.get('login_marker') == marker:
                # Original account identity and login evidence must still match;
                # a valid saved binding does not need a fresh composer every time.
                try:
                    gentoo_self_expected(profile)
                    return profile, marker
                except Exception:
                    pass
            try:
                proof = build_profile('teacherdeck-self')['profile']['contacts']['teacherdeck-self']
                identity = recipient_identity(proof['expected'])
                candidates = [cid for cid, contact in profile['contacts'].items()
                              if recipient_identity(contact.get('expected', '')) == identity]
                if len(candidates) != 1:
                    raise PrivateError('SELF_VERIFICATION_REQUIRED')
                profile = bind_gentoo_self(profile, candidates[0])
            except Exception:
                raise PrivateError('SELF_VERIFICATION_REQUIRED') from None
        return profile, marker

    def check(self, profile, marker):
        with com_session():
            if read_gentoo_login_marker(profile) != marker:
                raise PrivateError('CONNECTION_CHANGED')
            try:
                gentoo_self_expected(profile)
            except Exception:
                raise PrivateError('CONNECTION_CHANGED') from None


class PrivateAdapter:
    """One owner/session per process; the host additionally binds every handle."""
    def __init__(self, directory, *, provider=None, driver_factory=WindowsDriver,
                 context_factory=com_session):
        self.store = Store(Path(directory))
        self.provider = provider if provider is not None else GentooProvider()
        self.connection = None
        self.marker = None
        self.account_binding = None
        self.plans = {}
        self.runs = {}
        self.current_run = None
        self.guard = threading.RLock()
        adapter = self

        class BoundDriver:
            # Delegate actual input, F8 checks, scale/window guards and one
            # preparation recovery to the original driver and Runner.
            def __init__(self, profile, stop):
                self.profile = profile
                self.driver = driver_factory(profile, stop)

            def _check(self):
                adapter.provider.check(self.profile, adapter.marker)

            def prepare(self, job):
                self._check()
                self.driver.prepare(job)
                self._check()

            def verify(self, job):
                self._check()
                self.driver.verify(job)

            def verify_existing(self, job):
                self._check()
                method = getattr(self.driver, 'verify_existing', self.driver.verify)
                method(job)

            def checkpoint(self):
                self.driver.checkpoint()
                self._check()

            def submit(self, job):
                self._check()
                self.driver.submit(job)

            def verify_result(self, job):
                self._check()
                return self.driver.verify_result(job)

        self.runner = Runner(self.store, BoundDriver, context_factory)

    def close(self):
        self.runner.cancel()
        if self.runner.thread:
            self.runner.thread.join(2)
        # A blocked Windows call can still own SQLite. On EOF/process teardown
        # leave it to process termination; restart performs original recovery.
        if not self.runner.thread or not self.runner.thread.is_alive():
            self.store.close()

    def _connected(self, connection, *, live=True):
        if token(connection) != self.connection:
            raise PrivateError('INVALID_HANDLE')
        profile = self.store.setting('profile', {})
        if live:
            self.provider.check(profile, self.marker)
        return profile

    def _features(self, profile):
        features = []
        if any(not profile_errors(profile, cid, True) for cid in profile.get('contacts', {})):
            features.append('attachments')
        if profile.get('multi_select'):
            features.append('multi-recipient')
        if verification_available(profile):
            features.append('selected-result-verification')
        return features

    def connect(self, args):
        exact(args, ())
        if self.runner.busy():
            raise PrivateError('BUSY')
        profile, marker = self.provider.connect(self.store.setting('profile', {}))
        self.provider.check(profile, marker)
        self.store.set_setting('profile', profile)
        self.connection, self.marker = secrets.token_urlsafe(24), copy.deepcopy(marker)
        self.account_binding = hashlib.sha256(json.dumps(
            {'marker': marker, 'self': profile.get('self_binding')},
            sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        self.plans.clear()
        return {'connectionId': self.connection, 'capabilities': self._features(profile),
                'ready': True, 'versionSupport': 'supported', 'accountBinding': self.account_binding}

    def recipients(self, args):
        exact(args, ('connectionId',))
        profile = self._connected(args['connectionId'])
        result = []
        for cid, contact in profile.get('contacts', {}).items():
            identity = recipient_identity(contact.get('expected', ''))
            if identity[0] != 'gentoo_account':
                # Ambiguous labels must never become actionable recipients.
                continue
            parents = contact.get('selector', {}).get('parents', [])
            department = ' / '.join(p.get('name', '') for p in parents
                                    if isinstance(p, dict) and p.get('name'))
            result.append({'recipientId': cid, 'displayName': identity[1],
                           'department': department, 'accountLabel': identity[2]})
        if not result:
            raise PrivateError('RECIPIENT_MISMATCH')
        return result

    def _job(self, message, profile):
        exact(message, ('id', 'recipientIds', 'body', 'scheduledAt', 'attachmentPaths', 'contentRevision'), ('title',))
        token(message['id'])
        ids, paths = message['recipientIds'], message['attachmentPaths']
        title = '' if message.get('title') is None else message['title']
        body = message['body']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(cid, str) or cid not in profile.get('contacts', {}) for cid in ids)
                or len(set(ids)) != len(ids) or not isinstance(paths, list) or len(paths) > 10
                or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths)
                or len(set(paths)) != len(paths) or not isinstance(body, str)
                or not isinstance(title, str) or type(message['contentRevision']) is not int
                or not 0 <= message['contentRevision'] <= 0xffffffff
                or not isinstance(message['scheduledAt'], str)):
            raise PrivateError('INVALID_ARGS')
        contacts = profile['contacts']
        job = clean_job({'id': message['id'], 'recipient': ', '.join(contacts[c]['expected'] for c in ids),
                         'recipient_ids': ids, 'recipient_bindings': {
                             cid: {'selector': copy.deepcopy(contacts[cid]['selector']), 'expected': contacts[cid]['expected']}
                             for cid in ids}, 'title': title, 'body': body,
                         'scheduled': message['scheduledAt'], 'attachments': paths})
        if validate_job(job):
            raise PrivateError('INVALID_MESSAGE')
        if job['body'] != body or job['title'] != title:
            # Original clean_job normalizes drafts; never silently submit a
            # different string than the immutable TeacherDeck preview.
            raise PrivateError('INVALID_MESSAGE')
        return job

    def plan(self, args):
        exact(args, ('connectionId', 'messages', 'mode'), ('resultConfirmation',))
        profile = self._connected(args['connectionId'], live=args['mode'] != 'simulate')
        if self.runner.busy():
            raise PrivateError('BUSY')
        messages, mode = args['messages'], args['mode']
        confirmation = args.get('resultConfirmation') or 'manual'
        if (mode not in ('simulate', 'inspect', 'prepare', 'register')
                or confirmation not in ('manual', 'automatic')
                or not isinstance(messages, list) or not 1 <= len(messages) <= 500
                or mode in ('inspect', 'prepare') and len(messages) != 1
                or len(json.dumps(args, ensure_ascii=False).encode('utf-8')) > MAX_LINE):
            raise PrivateError('INVALID_ARGS')
        text_size = 0
        for m in messages:
            if not isinstance(m, dict) or not isinstance(m.get('body'), str) or m.get('title') is not None and not isinstance(m['title'], str):
                raise PrivateError('INVALID_ARGS')
            text_size += len((m.get('title') or '').encode('utf-8')) + len(m['body'].encode('utf-8'))
        if text_size > MAX_TEXT:
            raise PrivateError('INVALID_ARGS')
        jobs = [self._job(m, profile) for m in messages]
        if len({j['id'] for j in jobs}) != len(jobs):
            raise PrivateError('INVALID_ARGS')
        issues, items = [], []
        if confirmation == 'automatic' and not verification_available(profile):
            issues.append({'messageId': None, 'code': 'unsupported-feature'})
        for job in jobs:
            if job['attachments'] and 'attachments' not in self._features(profile):
                issues.append({'messageId': job['id'], 'code': 'unsupported-feature'})
            locked = equivalent_attempt(self.store, job, profile)
            try:
                current = self.store.get(job['id'])
                locked = locked or current['status'] in LOCKED
                state = STATES.get(current['status'], 'uncertain')
            except ValueError:
                current, state = None, 'draft'
            items.append({'messageId': job['id'], 'state': state, 'retryLocked': bool(locked)})
            if locked and mode in ('prepare', 'register'):
                issues.append({'messageId': job['id'], 'code': 'retry-locked'})
        plan_id = secrets.token_urlsafe(24)
        engine_id = None
        if not issues:
            # Reuse unchanged drafts/prepared messages; never reset a prepared
            # composer or locked ledger merely to make another preview.
            changing = []
            for job in jobs:
                try:
                    current = self.store.get(job['id'])
                except ValueError:
                    current = None
                if current is None or signature(current) != signature(job):
                    changing.append(job)
            if changing:
                self.store.save_batch(changing)
            result = self.runner.plan([j['id'] for j in jobs], mode, confirmation)
            engine_id = result['id']
            for error in result['errors']:
                issues.append({'messageId': error['id'], 'code': 'invalid-message'})
        self.plans = {k: p for k, p in self.plans.items() if time.monotonic() - p['at'] < 300}
        if len(self.plans) >= 16:
            raise PrivateError('BUSY')
        self.plans[plan_id] = {'at': time.monotonic(), 'engine': engine_id, 'issues': issues,
                               'ids': [j['id'] for j in jobs], 'mode': mode,
                               'connection': self.connection, 'marker': copy.deepcopy(self.marker),
                               'accountBinding': self.account_binding}
        expiry = (now().replace(tzinfo=timezone(timedelta(hours=9))) + timedelta(minutes=5)).isoformat(timespec='seconds')
        return {'planId': plan_id, 'expiresAt': expiry, 'issues': issues, 'items': items}

    def start(self, args):
        exact(args, ('planId', 'confirmed', 'accountBinding'))
        if type(args['confirmed']) is not bool:
            raise PrivateError('INVALID_ARGS')
        plan = self.plans.pop(token(args['planId']), None)
        if not plan or time.monotonic() - plan['at'] >= 300:
            raise PrivateError('PLAN_EXPIRED')
        self._connected(plan['connection'], live=plan['mode'] != 'simulate')
        if plan['issues'] or plan['marker'] != self.marker or not plan['engine']:
            raise PrivateError('PLAN_BLOCKED')
        if args['accountBinding'] != self.account_binding or plan['accountBinding'] != self.account_binding:
            raise PrivateError('CONNECTION_CHANGED')
        if plan['mode'] == 'register' and not args['confirmed']:
            raise PrivateError('CONFIRMATION_REQUIRED')
        if self.current_run:
            self.runs[self.current_run]['snapshot'] = self.runner.snapshot()
        self.runner.start(plan['engine'], '예약 등록' if args['confirmed'] else '')
        run_id = secrets.token_urlsafe(24)
        self.runs[run_id] = {'ids': plan['ids'], 'mode': plan['mode'], 'snapshot': None}
        self.current_run = run_id
        # Session handles expire; the durable original attempt ledger does not.
        if len(self.runs) > 32:
            self.runs.pop(next(iter(self.runs)))
        return {'runId': run_id}

    def status(self, args):
        exact(args, ('runId',))
        run_id = token(args['runId'])
        run = self.runs.get(run_id)
        if run is None:
            raise PrivateError('INVALID_HANDLE')
        snapshot = self.runner.snapshot() if run_id == self.current_run else run['snapshot']
        items = []
        for jid in run['ids']:
            job = self.store.get(jid)
            items.append({'messageId': jid, 'state': STATES.get(job['status'], 'uncertain'),
                           'retryLocked': job['status'] in LOCKED})
        stage = snapshot['stage'] if snapshot['stage'] in STAGES else 'failed'
        return {'active': bool(snapshot['active']) and run_id == self.current_run, 'mode': run['mode'],
                'stage': stage.replace('_', '-'), 'progress': snapshot['progress'], 'total': snapshot['total'],
                'items': items, 'issue': 'provider-failed' if stage == 'failed' else None}

    def cancel(self, args):
        exact(args, ('runId',))
        run_id = token(args['runId'])
        if run_id not in self.runs:
            raise PrivateError('INVALID_HANDLE')
        if run_id == self.current_run:
            self.runner.cancel()
        return {'requested': True}

    def dispatch(self, method, args):
        if method not in METHODS:
            raise PrivateError('INVALID_METHOD')
        with self.guard:
            return getattr(self, method)(args)


def handle_request(adapter, raw):
    """Decode a bounded request and redact every unexpected exception."""
    request_id = None
    try:
        if len(raw) > MAX_LINE or not raw.endswith(b'\n'):
            raise PrivateError('INVALID_ARGS')
        request = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        exact(request, ('id', 'method', 'args'))
        request_id = token(request['id'])
        if not isinstance(request['method'], str):
            raise PrivateError('INVALID_METHOD')
        result = adapter.dispatch(request['method'], request['args'])
        response = {'id': request_id, 'ok': True, 'result': result}
        if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) + 1 > MAX_LINE:
            raise PrivateError('RESULT_TOO_LARGE')
    except PrivateError as exc:
        response = {'id': request_id, 'ok': False, 'error': {'code': exc.code}}
    except Exception:
        response = {'id': request_id, 'ok': False, 'error': {'code': 'PROVIDER_FAILED'}}
    return json.dumps(response, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'


def serve(adapter, input_stream, output_stream):
    """Sequential framed requests; oversized frames terminate rather than drain."""
    while True:
        raw = input_stream.readline(MAX_LINE + 1)
        if not raw:
            break
        if len(raw) > MAX_LINE or not raw.endswith(b'\n'):
            output_stream.write(b'{"id":null,"ok":false,"error":{"code":"INVALID_ARGS"}}\n')
            output_stream.flush()
            break
        output_stream.write(handle_request(adapter, raw))
        output_stream.flush()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PrivateError('INVALID_ARGS')
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description='Fixed TeacherDeck private worker')
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    directory = Path(args.data_dir)
    if not directory.is_absolute():
        return 2
    # Original COM MTA lifetime is preserved. Suppress dependency chatter on
    # both streams; the sole stdout is a bounded structured private response.
    input_stream, output_stream = sys.stdin.buffer, sys.stdout.buffer
    dll_dir = Path(sys.executable).parent / 'Lib/site-packages/pywin32_system32'
    dll_context = os.add_dll_directory(str(dll_dir)) if sys.platform == 'win32' else contextlib.nullcontext()
    with contextlib.redirect_stdout(_NullWriter()), contextlib.redirect_stderr(_NullWriter()), dll_context:
        # Configure generated COM wrappers before pywinauto imports UIA.
        import comtypes.client
        comtypes.client.gen_dir = None
        with automation_runtime():
            adapter = PrivateAdapter(directory)
            try:
                serve(adapter, input_stream, output_stream)
            finally:
                adapter.close()
    return 0


class _NullWriter:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


if __name__ == '__main__':
    raise SystemExit(main())
