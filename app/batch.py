"""Atomic, reviewable batch input. This service never controls the messenger."""
from __future__ import annotations

import copy
import csv
import io
import json
import secrets
import time

from .core import MAX_JOBS, clean_job, signature, validate_job
from .importers import read_xlsx
from .recipients import resolve, content_key, equivalent_attempt

HEADERS = ['받는사람', '예약날짜', '예약시간', '제목', '내용', '첨부파일']


def table_file(name, data):
    if len(data) > 10 * 1024 * 1024:
        raise ValueError('파일은 10MB 이하여야 합니다.')
    if name.lower().endswith('.xlsx'):
        rows = read_xlsx(data)
    elif name.lower().endswith('.csv'):
        for encoding in ('utf-8-sig', 'cp949'):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError('CSV는 UTF-8 또는 CP949로 저장하세요.')
        csv.field_size_limit(1024 * 1024)
        try:
            rows = list(csv.reader(io.StringIO(text), strict=True))
        except csv.Error as exc:
            raise ValueError('CSV의 따옴표와 행 구분을 확인하세요. 파일을 다시 저장해 주세요.') from exc
    else:
        raise ValueError('.xlsx 또는 .csv 파일을 선택하세요.')
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        raise ValueError('파일이 비어 있습니다.')
    headers = [str(cell).strip().replace(' ', '') for cell in rows[0]]
    if len(headers) != len(set(headers)):
        raise ValueError('중복된 열 이름이 있습니다.')
    if not {'받는사람', '내용'}.issubset(headers) or not ('예약일시' in headers or {'예약날짜', '예약시간'}.issubset(headers)):
        raise ValueError('받는사람, 예약날짜, 예약시간, 내용 열이 필요합니다. 제목은 선택 사항이며 기존 예약일시 열도 사용할 수 있습니다.')
    if len(rows) - 1 > MAX_JOBS:
        raise ValueError('한 번에 500건까지 입력할 수 있습니다.')
    for index, row in enumerate(rows[1:], 2):
        if any(str(cell).strip() for cell in row[len(headers):]):
            raise ValueError(f'{index}행: 열 이름보다 값이 많습니다. 본문의 쉼표는 따옴표로 묶고 열 구분을 확인하세요.')
    return [dict(zip(headers, row)) for row in rows[1:]]


def scheduled(raw):
    if raw.get('scheduled') or raw.get('예약일시'):
        return raw.get('scheduled') or raw['예약일시']
    day = str(raw.get('date', raw.get('예약날짜', ''))).strip()
    clock = str(raw.get('time', raw.get('예약시간', ''))).strip()
    if ' ' in day:
        day = day.split(' ')[0]
    try:
        number = float(clock)
        if 0 <= number < 1:
            minutes = round(number * 1440)
            if not 0 <= minutes < 1440:
                raise ValueError('예약시간을 확인하세요.')
            clock = f'{minutes // 60:02}:{minutes % 60:02}'
    except ValueError:
        pass
    return day + ' ' + clock


class BatchService:
    def __init__(self, store):
        self.store, self.reviews = store, {}

    def preview(self, raw_rows, choices=None):
        if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= MAX_JOBS or any(not isinstance(r, dict) for r in raw_rows):
            raise ValueError('메시지를 1~500행 입력하세요.')
        if choices is not None and not isinstance(choices, dict):
            raise ValueError('수신자 선택 정보가 잘못되었습니다.')
        profile = self.store.setting('profile', {'roles': {}, 'contacts': {}})
        existing = {content_key(job, profile) for job in self.store.list_jobs()}
        seen, rows, jobs = set(), [], []
        for index, raw in enumerate(raw_rows):
            row = {'row': index + 1, 'input': copy.deepcopy(raw), 'issues': [], 'selections': []}
            try:
                selected = (choices or {}).get(str(index), {})
                if not isinstance(selected, dict):
                    raise ValueError('수신자 선택 정보가 잘못되었습니다.')
                result = resolve(raw.get('recipient', raw.get('받는사람', '')), profile.get('contacts', {}), selected)
                row['selections'], row['issues'] = result['selections'], result['issues']
                job_raw = {'scheduled': scheduled(raw), 'recipient': ', '.join(result['names']),
                           'title': raw.get('title', raw.get('제목', '')),
                           'body': raw.get('body', raw.get('내용', '')),
                           'attachments': raw.get('attachments', raw.get('첨부파일', ''))}
                if not result['issues']:
                    job_raw.update(recipient_ids=result['ids'], recipient_bindings=result['bindings'])
                job = clean_job(job_raw)
                row['job'] = job
                row['issues'] += [{'code': 'validation', 'message': message} for message in validate_job(job)]
                if not result['issues']:
                    key = content_key(job, profile)
                    if key in seen or key in existing or equivalent_attempt(self.store, job, profile):
                        row['issues'].append({'code': 'duplicate', 'message': '같은 수신자·시각·내용의 초안 또는 등록 시도 기록이 있습니다.'})
                    seen.add(key)
                if not row['issues']:
                    jobs.append(job)
            except (ValueError, TypeError) as exc:
                row['issues'].append({'code': 'validation', 'message': str(exc)})
            rows.append(row)
        token = secrets.token_urlsafe(24)
        self.reviews = {k: v for k, v in self.reviews.items() if time.monotonic() - v['at'] < 300}
        if len(self.reviews) >= 24:
            self.reviews.pop(next(iter(self.reviews)))
        self.reviews[token] = {'at': time.monotonic(), 'profile': copy.deepcopy(profile), 'jobs': jobs, 'rows': rows}
        return {'review_id': token, 'rows': rows, 'total': len(rows), 'ready': len(jobs),
                'can_commit': len(jobs) == len(rows), 'expires_in': 300}

    def commit(self, review_id):
        review = self.reviews.get(review_id)
        if not review or time.monotonic() - review['at'] > 300:
            raise ValueError('검토가 만료되었습니다. 다시 미리보기 해주세요.')
        if len(review['jobs']) != len(review['rows']):
            raise ValueError('모든 행의 오류와 수신자를 먼저 확인하세요.')
        profile = self.store.setting('profile', {'roles': {}, 'contacts': {}})
        if profile != review['profile']:
            raise ValueError('수신자 연결이 변경되었습니다. 다시 미리보기 해주세요.')
        existing = {content_key(job, profile) for job in self.store.list_jobs()}
        for job in review['jobs']:
            if validate_job(job) or equivalent_attempt(self.store, job, profile) or content_key(job, profile) in existing:
                raise ValueError('시간 또는 등록 기록이 변경되었습니다. 다시 미리보기 해주세요.')
        saved = self.store.save_batch(review['jobs'])
        del self.reviews[review_id]
        return {'saved': len(saved), 'job_ids': [job['id'] for job in saved]}
