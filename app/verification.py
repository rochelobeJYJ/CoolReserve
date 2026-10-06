"""Reservation receipts must match the entire message, never only its title."""
from __future__ import annotations

from pathlib import Path
import re

from .core import normalize, parse_time
from .windows import body_matches, recipient_list_matches

RESULT_ROLES = {'sent_list', 'result_title', 'result_body', 'result_recipient', 'result_datetime',
                'result_reserved', 'result_attachments'}


def available(profile, attachments=False):
    roles = profile.get('roles', {})
    # A message with no expected files still needs proof of an empty inventory.
    return RESULT_ROLES.issubset(roles)


def parse_receipt_time(value):
    """Accept explicit 24-hour dates, including Gentoo's observed weekday suffix."""
    match = re.fullmatch(
        r'([0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2} [0-9]{1,2}:[0-9]{2}(?::00)?)'
        r'(?: \(([월화수목금토일])\))?', normalize(value))
    if not match:
        raise ValueError('예약 결과 시각의 24시간 표시 형식을 확인할 수 없습니다.')
    result = parse_time(match[1])
    if match[2] and match[2] != '월화수목금토일'[result.weekday()]:
        raise ValueError('예약 결과의 날짜와 요일이 일치하지 않습니다.')
    return result


def match_receipts(records, job, expected, *, complete=False):
    matches = []
    unreadable = False
    for record in records:
        try:
            if not isinstance(record['title'], str):
                raise ValueError('예약 결과의 제목을 정확히 읽을 수 없습니다.')
            attachments = record['attachments']
            if (not isinstance(attachments, list)
                    or any(not isinstance(name, str) or not name.strip() for name in attachments)):
                raise ValueError('예약 결과의 첨부 목록을 확인할 수 없습니다.')
            okay = (normalize(record['title']) == normalize(job['title'])
                    and body_matches(record['body'], job['body'])
                    and recipient_list_matches(record['recipient'], expected)
                    and parse_receipt_time(record['scheduled']) == parse_time(job['scheduled'])
                    and record['reserved'] is True
                    and sorted(name.casefold() for name in attachments)
                    == sorted(Path(p).name.casefold() for p in (job.get('attachments') or [])))
            if okay:
                matches.append(record)
        except (KeyError, TypeError, ValueError):
            unreadable = True
    if len(matches) > 1:
        return {'confirmed': False, 'code': 'ambiguous', 'message': '같은 예약이 여러 건 있습니다. 재등록하지 말고 목록을 확인하세요.'}
    if unreadable:
        return {'confirmed': False, 'code': 'unreadable', 'message': '예약 목록을 끝까지 정확히 읽지 못했습니다. 재등록하지 말고 확인하세요.'}
    if len(matches) == 1 and complete:
        return {'confirmed': True, 'code': 'confirmed', 'message': '예약 목록의 수신자·시각·제목·내용이 정확히 한 건과 일치합니다.'}
    return {'confirmed': False, 'code': 'not_found' if complete else 'unverified',
            'message': '일치하는 예약을 확인하지 못했습니다. 재등록하지 말고 목록을 확인하세요.'}
