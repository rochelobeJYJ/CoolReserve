"""Resolve user-entered names locally; ambiguous matches always require a choice."""
from __future__ import annotations

import copy
import re
import unicodedata

from .core import LOCKED, normalize, recipient_ids, signature
from .windows import recipient_identity


def names(value):
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r'[,，;\r\n]', str(value or ''))
    result = [unicodedata.normalize('NFC', normalize(part)) for part in parts if normalize(part)]
    if not result:
        raise ValueError('받는 사람을 입력하세요.')
    if len(result) > 100:
        raise ValueError('한 메시지의 수신자는 100명 이하여야 합니다.')
    if len(result) != len(set(result)):
        raise ValueError('같은 수신자 이름이 중복돼 있습니다.')
    if any('\t' in part or '\x00' in part for part in result):
        raise ValueError('수신자 이름에 탭이나 널 문자를 사용할 수 없습니다.')
    return result


def aliases(key, contact):
    values = [key, contact.get('display_name'), contact.get('expected')]
    extra = contact.get('source_names', [])
    values += [extra] if isinstance(extra, str) else extra
    for value in (contact.get('display_name'), contact.get('expected')):
        identity = recipient_identity(value or '')
        if identity[0] == 'gentoo_account':
            values.append(identity[1])
    return {unicodedata.normalize('NFC', normalize(v)) for v in values if normalize(v)}


def resolve(value, contacts, choices=None):
    requested = names(value)
    choices = choices or {}
    ids, bindings, selections, issues = [], {}, [], []
    for name in requested:
        candidates = [key for key, c in contacts.items() if isinstance(c, dict) and name in aliases(key, c)]
        chosen = choices.get(name)
        if chosen is not None and chosen not in candidates:
            issues.append({'code': 'invalid_choice', 'name': name, 'message': '선택한 계정이 검색 결과와 다릅니다.'})
            chosen = None
        elif chosen is None and len(candidates) == 1:
            chosen = candidates[0]
        selection = {'name': name, 'selected_id': chosen,
                     'candidates': [{'id': key, 'label': contacts[key].get('display_name') or key,
                                     'expected': contacts[key].get('expected', '')} for key in candidates]}
        selections.append(selection)
        if chosen is None:
            issues.append({'code': 'ambiguous' if candidates else 'not_found', 'name': name,
                           'message': '동명이인입니다. 소속과 전체 표시를 보고 선택하세요.' if candidates else '일치하는 계정이 없습니다. 연결과 이름을 확인하세요.'})
            continue
        contact = contacts[chosen]
        if not contact.get('selector') or not normalize(contact.get('expected')) or contact.get('verified') is False:
            issues.append({'code': 'unbound', 'name': name, 'message': '실제 수신자 연결이 필요합니다.'})
            continue
        if chosen in ids:
            issues.append({'code': 'duplicate_account', 'name': name, 'message': '서로 다른 이름이 같은 계정을 가리킵니다.'})
            continue
        ids.append(chosen)
        bindings[chosen] = {'selector': copy.deepcopy(contact['selector']), 'expected': contact['expected']}
    return {'names': requested, 'ids': ids, 'bindings': bindings, 'selections': selections, 'issues': issues}


def account_set(job, profile):
    """Saved binding evidence takes precedence over a subsequently changed contact."""
    contacts = profile.get('contacts', {})
    identities = []
    for key in recipient_ids(job):
        evidence = job.get('recipient_bindings', {}).get(key) or contacts.get(key, {})
        expected = evidence.get('expected')
        if expected:
            for value in normalize(expected).splitlines():
                identity = recipient_identity(value)
                identities.append(('gentoo_account', identity[2]) if identity[0] == 'gentoo_account' else identity)
        else:
            identities.append(('binding', key))
    return tuple(sorted(identities))


def content_key(job, profile):
    return (job['scheduled'], normalize(job['title']), normalize(job['body']),
            tuple(sorted(str(p).replace('\\', '/').casefold() for p in job.get('attachments', []))), account_set(job, profile))


def equivalent_attempt(store, job, profile):
    """Preserve old attempt fingerprints and block reimport under a new display label."""
    if store.has_attempt(signature(job)):
        return True
    with store.lock:
        if store.db.execute('SELECT 1 FROM attempts a LEFT JOIN jobs j ON a.job_id=j.id WHERE j.id IS NULL LIMIT 1').fetchone():
            return True
    if 'recipient_ids' in job and len(job['recipient_ids']) == 1:
        identity = account_set(job, profile)
        for alias, contact in profile.get('contacts', {}).items():
            probe = dict(job, recipient=alias)
            probe.pop('recipient_ids', None)
            probe.pop('recipient_bindings', None)
            if account_set(probe, profile) == identity and store.has_attempt(signature(probe)):
                return True
    target = content_key(job, profile)
    return any(old['id'] != job.get('id') and (old['status'] in LOCKED or store.has_attempt(signature(old)))
               and content_key(old, profile) == target for old in store.list_jobs())
