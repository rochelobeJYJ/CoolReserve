"""Read-only source compilation and atomic local draft commits.

This module never reads Google, controls CoolMessenger or registers messages.
Source values and explicit contact mappings are provided by the caller.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import string
import uuid
from datetime import date, datetime, timedelta

from .core import LOCKED, MAX_JOBS, clean_job, normalize, signature, stamp, validate_job


LUNCH = {
    'id': 'lunch', 'preset': 'lunch', 'name': '중식 지도', 'version': 1,
    'header_rows': 2, 'grouping': 'date',
    'columns': {'date': '날짜', 'first': '최종_중식1', 'second': '최종_중식2',
                'exclude': '제외일', 'first_only': '고사(1차만)'},
    'send': {'offset_days': 0, 'time': '09:05'},
    'slots': [{'id': '중식1', 'label': '1차', 'time': '12:20~12:40'},
              {'id': '중식2', 'label': '2차', 'time': '12:40~13:00'}],
    'title_template': '[중식 지도] {지도일}({요일}) 안내',
    'body_template': '안녕하세요, 선생님.\n{지도일}({요일}) 중식 지도 일정을 안내드립니다.\n\n'
                     '{담당목록}\n\n시간에 맞춰 지도해 주시면 감사하겠습니다.\n'
                     '오늘도 좋은 하루 보내세요. 감사합니다!',
    'contact_mapping': {},
}
GENERAL = {
    'id': 'general', 'preset': 'general', 'name': '일반 업무 안내', 'version': 1,
    'header_rows': 1, 'grouping': 'row',
    'columns': {'date': '업무일', 'recipient': '담당자', 'title': '제목', 'body': '내용',
                'id': '', 'exclude': ''},
    'send': {'offset_days': 0, 'time': '09:05'}, 'slots': [],
    'title_template': '{제목}', 'body_template': '{내용}', 'contact_mapping': {},
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _content(job):
    return {k: job.get(k) for k in ('scheduled', 'recipient', 'recipient_ids', 'recipient_bindings',
                                   'title', 'body', 'attachments')}


def _render(template, variables):
    out = []
    try:
        for literal, field, spec, conversion in string.Formatter().parse(template):
            out.append(literal)
            if field is None:
                continue
            if spec or conversion or any(c in field for c in '.[]') or field not in variables:
                raise ValueError(f'알 수 없거나 지원하지 않는 문구 변수: {{{field}}}')
            value = variables[field]
            out.append(' · '.join(str(x) for x in value) if isinstance(value, list) else str(value if value is not None else ''))
    except (ValueError, KeyError, IndexError) as exc:
        raise ValueError(str(exc)) from exc
    return normalize(''.join(out))


def _boolean(value):
    if type(value) is bool:
        return value
    if value in ('', None, 'FALSE', 'false', 0):
        return False
    if value in ('TRUE', 'true', 1):
        return True
    raise ValueError('제외일·고사 표시는 TRUE 또는 FALSE여야 합니다.')


def _day(value, year=None, month=None):
    if isinstance(value, bool):
        raise ValueError('날짜에 체크박스 값이 있습니다.')
    if isinstance(value, (int, float)) and 10000 < value < 200000:
        return (datetime(1899, 12, 30) + timedelta(days=int(value))).date()
    text = normalize(value)
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    if year and month and re.fullmatch(r'\d{1,2}', text):
        return date(int(year), int(month), int(text))
    if year and re.fullmatch(r'\d{1,2}/\d{1,2}', text):
        m, d = map(int, text.split('/'))
        return date(int(year), m, d)
    raise ValueError('업무 날짜는 연도를 포함한 날짜 또는 날짜 숫자값이어야 합니다.')


def _names(value):
    if isinstance(value, list):
        names = [normalize(x) for x in value]
    else:
        names = [normalize(x) for x in re.split(r'[,;\n]', str(value or ''))]
    names = [x for x in names if x]
    if not names:
        raise ValueError('최종 담당자가 비어 있습니다.')
    if len(names) != len(set(names)):
        raise ValueError('같은 담당자가 한 업무에 중복돼 있습니다.')
    return names


class WorkflowService:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.executescript('''
            CREATE TABLE IF NOT EXISTS workflow_recipes(
              id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_snapshots(
              id TEXT PRIMARY KEY, source_key TEXT NOT NULL, value_hash TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_previews(
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_items(
              logical_key TEXT PRIMARY KEY, family TEXT NOT NULL, day TEXT NOT NULL, job_id TEXT NOT NULL,
              occurrence_keys TEXT NOT NULL, render_hash TEXT NOT NULL, payload TEXT NOT NULL,
              snapshot_id TEXT NOT NULL, recipe_version INTEGER NOT NULL, state TEXT NOT NULL);
            ''')

    def presets(self):
        return [{'id': value['id'], 'name': value['name'], 'recipe': copy.deepcopy(value)}
                for value in (LUNCH, GENERAL)]

    def list_recipes(self):
        with self.store.lock:
            rows = self.store.db.execute('SELECT payload FROM workflow_recipes ORDER BY updated,id').fetchall()
        recipes = {value['id']: copy.deepcopy(value) for value in (LUNCH, GENERAL)}
        recipes.update({value['id']: value for value in (json.loads(row['payload']) for row in rows)})
        return list(recipes.values())

    def _recipe(self, body):
        if isinstance(body, str):
            with self.store.lock:
                row = self.store.db.execute('SELECT payload FROM workflow_recipes WHERE id=?', (body,)).fetchone()
            if row:
                return json.loads(row['payload'])
            body = {'preset': body}
        if not isinstance(body, dict):
            raise ValueError('업무 양식 형식이 잘못되었습니다.')
        preset = body.get('preset', body.get('id', 'lunch'))
        if preset not in ('lunch', 'general'):
            raise ValueError('지원하지 않는 업무 양식입니다.')
        cfg = copy.deepcopy(LUNCH if preset == 'lunch' else GENERAL)
        for key, value in body.items():
            if key in ('columns', 'send'):
                if not isinstance(value, dict):
                    raise ValueError(f'{key} 설정은 객체여야 합니다.')
                cfg[key].update(value)
            else:
                cfg[key] = copy.deepcopy(value)
        cfg['preset'] = preset
        cfg['id'] = normalize(cfg.get('id')) or preset
        cfg['name'] = normalize(cfg.get('name')) or cfg['id']
        if cfg['grouping'] not in ('date', 'row'):
            raise ValueError('묶음 방식은 date 또는 row여야 합니다.')
        if type(cfg['header_rows']) is not int or not 1 <= cfg['header_rows'] <= 10:
            raise ValueError('헤더 행 수는 1~10이어야 합니다.')
        offset = cfg['send'].get('offset_days')
        if type(offset) is not int or not -90 <= offset <= 90:
            raise ValueError('발송 날짜 차이는 -90~90의 정수여야 합니다.')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(cfg['send'].get('time', ''))):
            raise ValueError('발송시각은 HH:MM 형식이어야 합니다.')
        if not isinstance(cfg.get('contact_mapping'), dict):
            raise ValueError('담당자 연결은 이름과 계정 ID의 객체여야 합니다.')
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in cfg['contact_mapping'].items()):
            raise ValueError('담당자 연결의 이름과 계정 ID는 문자열이어야 합니다.')
        if not isinstance(cfg.get('title_template'), str):
            raise ValueError('제목 양식은 문자열이어야 합니다. 제목 없이 보내려면 빈칸으로 두세요.')
        if not isinstance(cfg.get('body_template'), str) or not normalize(cfg['body_template']):
            raise ValueError('본문 양식이 비어 있습니다.')
        if preset == 'lunch':
            if len(cfg.get('slots', [])) != 2 or any(not all(normalize(slot.get(k)) for k in ('id', 'label', 'time'))
                                                    for slot in cfg['slots']):
                raise ValueError('중식의 1·2차 지도 시간 설정을 확인하세요.')
            if len({slot['id'] for slot in cfg['slots']}) != 2:
                raise ValueError('차시 ID는 서로 달라야 합니다.')
        return cfg

    def save_recipe(self, body):
        cfg = self._recipe(body.get('recipe', body) if isinstance(body, dict) else body)
        with self.store.lock, self.store.db:
            old = self.store.db.execute('SELECT payload,version FROM workflow_recipes WHERE id=?', (cfg['id'],)).fetchone()
            compare = dict(cfg, version=0)
            cfg['version'] = (old['version'] + (_json(dict(json.loads(old['payload']), version=0)) != _json(compare))) if old else 1
            self.store.db.execute('INSERT OR REPLACE INTO workflow_recipes VALUES (?,?,?,?)',
                                  (cfg['id'], cfg['version'], _json(cfg), stamp()))
        return cfg

    def _snapshot(self, snapshot):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('values'), list):
            raise ValueError('자료 스냅샷에 values 배열이 필요합니다.')
        if len(snapshot['values']) > MAX_JOBS + 10 or any(not isinstance(row, list) for row in snapshot['values']):
            raise ValueError('자료 행은 배열이며 최대 500개 업무만 지원합니다.')
        source = copy.deepcopy(snapshot.get('source', {}))
        if not isinstance(source, dict):
            raise ValueError('자료 위치 형식이 잘못되었습니다.')
        identity = source.get('source_id') or source.get('spreadsheet_id') or source.get('path') or snapshot.get('source_id')
        if not identity:
            raise ValueError('반복 읽기를 식별할 자료 ID가 필요합니다.')
        source_key = _hash({'kind': source.get('kind', 'values'), 'id': str(identity),
                            'tab_id': source.get('tab_id', source.get('sheet_id', source.get('sheet', '')))})
        value_hash = _hash(snapshot['values'])
        return {'id': snapshot.get('snapshot_id') or snapshot.get('id') or uuid.uuid4().hex,
                'source': source, 'source_key': source_key, 'value_hash': value_hash,
                'values': copy.deepcopy(snapshot['values']), 'read_at': snapshot.get('read_at') or stamp()}

    def _records(self, snapshot, cfg, start, end):
        values = snapshot['values']
        h = cfg['header_rows']
        if len(values) < h:
            raise ValueError('설정한 헤더 행이 자료에 없습니다.')
        headers = [normalize(x) for x in values[h - 1]]
        if any(not x for x in headers) or len(headers) != len(set(headers)):
            raise ValueError('자료 헤더가 비었거나 중복돼 있습니다.')
        required = ('date', 'first', 'second', 'exclude', 'first_only') if cfg['preset'] == 'lunch' else ('date', 'recipient')
        for key in required:
            if cfg['columns'].get(key) not in headers:
                raise ValueError(f'연결한 열을 찾을 수 없습니다: {cfg["columns"].get(key, key)}')
        for key, column in cfg['columns'].items():
            if key != 'title' and column and column not in headers:
                raise ValueError(f'연결한 열을 찾을 수 없습니다: {column}')
        year, month = cfg.get('year'), cfg.get('month')
        lunch_month = None
        if cfg['preset'] == 'lunch':
            # Header-first templates contain complete dates. Only a separate
            # metadata row may supply a year/month for legacy numeric day rows.
            year = month = None
            first = values[0] if h > 1 else []
            if any(normalize(value) in ('연도', '월') for value in first):
                meta = {normalize(first[i]): first[i + 1] for i in range(len(first) - 1)
                        if normalize(first[i]) in ('연도', '월')}
                try:
                    year, month = int(meta['연도']), int(meta['월'])
                    date(year, month, 1)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError('첫 행의 연도·월을 확인하세요.') from exc
                lunch_month = (year, month)
        records, errors, excluded = [], [], 0
        for index, cells in enumerate(values[h:], h + 1):
            if not any(normalize(x) for x in cells):
                continue
            row = {name: cells[i] if i < len(cells) else '' for i, name in enumerate(headers)}
            try:
                day = _day(row[cfg['columns']['date']], year, month)
                if lunch_month is not None and (day.year, day.month) != lunch_month:
                    raise ValueError('업무 날짜와 첫 행의 연도·월이 다릅니다.')
                if not start <= day <= end:
                    continue
                exclude_col = cfg['columns'].get('exclude')
                if exclude_col and _boolean(row[exclude_col]):
                    excluded += 1
                    continue
                if cfg['preset'] == 'lunch':
                    first_only = _boolean(row[cfg['columns']['first_only']])
                    for slot_index in range(1 if first_only else 2):
                        slot = cfg['slots'][slot_index]
                        key = 'first' if slot_index == 0 else 'second'
                        names = _names(row[cfg['columns'][key]])
                        if len(names) != 1:
                            raise ValueError('한 지도 차시에는 최종 담당자 한 명이 필요합니다.')
                        records.append({'day': day.isoformat(), 'slot': slot['id'], 'label': slot['label'],
                                        'time': slot['time'], 'names': names, 'row': row, 'source_row': index})
                else:
                    identity_col = cfg['columns'].get('id')
                    # Without an explicit task ID, identify the row by its
                    # content rather than its position. Reordering then keeps
                    # drafts stable; an edit is reviewed as removal + addition.
                    record_id = normalize(row[identity_col]) if identity_col else 'row:' + _hash(row)
                    if not record_id:
                        raise ValueError('업무 ID가 비어 있습니다.')
                    records.append({'day': day.isoformat(), 'slot': record_id,
                                    'label': record_id if identity_col else '업무', 'time': '',
                                    'names': _names(row[cfg['columns']['recipient']]), 'row': row, 'source_row': index})
            except (ValueError, TypeError, KeyError) as exc:
                errors.append({'reason': str(exc), 'source_rows': [index], 'code': 'invalid_source'})
        return records, errors, excluded

    def _contacts(self, cfg, profile):
        contacts = profile.get('contacts', {})
        mapping = dict(cfg['contact_mapping'])
        candidates = {}
        for contact_id, contact in contacts.items():
            if not isinstance(contact, dict):
                continue
            source_names = contact.get('source_names', []) or []
            if isinstance(source_names, str):
                source_names = [source_names]
            for name in source_names:
                candidates.setdefault(normalize(name), []).append(contact_id)
        for name, ids in candidates.items():
            if name not in mapping and len(set(ids)) == 1:
                mapping[name] = ids[0]
        return contacts, mapping

    def _get_job(self, job_id):
        try:
            return self.store.get(job_id)
        except ValueError:
            return None

    def _attempted(self, item):
        job = self._get_job(item['job_id'])
        with self.store.lock:
            history = self.store.db.execute('SELECT 1 FROM attempts WHERE job_id=? LIMIT 1',
                                             (item['job_id'],)).fetchone()
        return bool(history or (job and (job.get('status') in LOCKED or self.store.has_attempt(signature(job)))))

    def preview(self, snapshot, recipe, from_date, to_date):
        cfg, snap = self._recipe(recipe), self._snapshot(snapshot)
        start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
        if start > end:
            raise ValueError('기간의 끝이 시작보다 빠릅니다.')
        records, blocked, excluded = self._records(snap, cfg, start, end)
        profile = self.store.setting('profile', {'contacts': {}})
        contacts, mapping = self._contacts(cfg, profile)
        family = 'lunch' if cfg['preset'] == 'lunch' else f'general:{cfg["id"]}'
        groups, seen_occurrences = {}, set()
        for record in records:
            occurrence = f'{family}:{record["day"]}:{record["slot"]}'
            if occurrence in seen_occurrences:
                blocked.append({'reason': '같은 날짜·차시의 업무가 중복돼 있습니다.', 'source_rows': [record['source_row']],
                                'code': 'duplicate_occurrence', 'occurrence_keys': [occurrence]})
            seen_occurrences.add(occurrence)
            record['occurrence_key'] = occurrence
            group_key = record['day'] if cfg['grouping'] == 'date' else f'{record["day"]}:{record["slot"]}'
            groups.setdefault(group_key, []).append(record)
        with self.store.lock:
            rows = self.store.db.execute('SELECT * FROM workflow_items WHERE family=? AND day>=? AND day<=?',
                                         (family, start.isoformat(), end.isoformat())).fetchall()
        old = {row['logical_key']: dict(row) for row in rows}
        # A shorter range or another tab is not evidence that tasks were
        # removed from the original source. Keep its linked drafts intact.
        with self.store.lock:
            previous_scopes = {}
            for item in old.values():
                if item['state'] != 'active':
                    continue
                sid = item['snapshot_id']
                if sid not in previous_scopes:
                    saved = self.store.db.execute('SELECT payload FROM workflow_snapshots WHERE id=?', (sid,)).fetchone()
                    previous_scopes[sid] = json.loads(saved['payload']) if saved else None
                prior = previous_scopes[sid]
                if (not prior or prior['source_key'] != snap['source_key']
                        or normalize(prior['source'].get('range', '')).upper() != normalize(snap['source'].get('range', '')).upper()):
                    blocked.append({'reason': '기존 초안의 자료·탭 또는 읽기 범위와 다릅니다. 같은 자료 범위로 다시 읽어주세요.',
                                    'code': 'source_scope_changed', 'job_id': item['job_id'],
                                    'source_rows': json.loads(item['payload']).get('source_rows', [])})
        diff = {key: [] for key in ('added', 'changed', 'deleted', 'unchanged')}
        candidates, unmatched = [], set()
        new_keys = set()
        for group_key, tasks in groups.items():
            logical = _hash({'family': family, 'group': group_key})
            new_keys.add(logical)
            day = date.fromisoformat(tasks[0]['day'])
            # A person may own multiple distinct tasks in a daily message.
            # Preserve every task in the body but add each named recipient once.
            names = list(dict.fromkeys(name for task in tasks for name in task['names']))
            issues, recipient_ids, bindings = [], [], {}
            for name in names:
                contact_id = mapping.get(name)
                contact = contacts.get(contact_id, {})
                if (not contact_id or not isinstance(contact, dict) or not isinstance(contact.get('selector'), dict)
                        or not contact.get('selector') or not normalize(contact.get('expected'))
                        or contact.get('verified') is False):
                    unmatched.add(name)
                    issues.append(f'실제 계정 연결 필요: {name}')
                    continue
                if contact_id in recipient_ids:
                    issues.append('서로 다른 원본 이름이 같은 계정에 연결돼 있습니다.')
                recipient_ids.append(contact_id)
                bindings[contact_id] = {'selector': copy.deepcopy(contact['selector']), 'expected': contact['expected']}
            variables = {key: value for key, value in tasks[0]['row'].items()}
            variables.update({'날짜': day.isoformat(), '지도일ISO': day.isoformat(),
                              '지도일': f'{day.month}월 {day.day}일', '업무일': f'{day.month}월 {day.day}일',
                              '요일': '월화수목금토일'[day.weekday()], '받는사람': ' · '.join(names),
                              '담당목록': '\n'.join(f'{task["label"]}: {task["time"]} {" · ".join(task["names"])} 선생님'
                                                 for task in tasks)})
            for key, alias in (('title', '제목'), ('body', '내용')):
                column = cfg['columns'].get(key)
                if column or key == 'title':
                    variables[alias] = tasks[0]['row'].get(column, '')
            previous = old.get(logical)
            job_id = previous['job_id'] if previous and previous['state'] == 'active' else uuid.uuid4().hex
            raw = {'id': job_id, 'scheduled': f'{(day + timedelta(days=cfg["send"]["offset_days"])).isoformat()} {cfg["send"]["time"]}',
                   'recipient': ' · '.join(names), 'recipient_ids': sorted(set(recipient_ids)),
                   'recipient_bindings': bindings, 'attachments': []}
            try:
                raw.update(title=_render(cfg['title_template'], variables), body=_render(cfg['body_template'], variables))
                job = clean_job(raw)
            except ValueError as exc:
                job = dict(raw)
                job.setdefault('title', '')
                job.setdefault('body', '')
                issues.append(str(exc))
            issues.extend(validate_job(job))
            render_hash = _hash(_content(job))
            change = 'added' if not previous or previous['state'] == 'deleted' else (
                'unchanged' if previous['render_hash'] == render_hash else 'changed')
            summary = {'logical_key': logical, 'day': day.isoformat(), 'job_id': job_id,
                       'source_rows': sorted({task['source_row'] for task in tasks})}
            diff[change].append(summary)
            occurrence_keys = [task['occurrence_key'] for task in tasks]
            for item in old.values():
                overlap = set(occurrence_keys).intersection(json.loads(item['occurrence_keys']))
                if overlap and self._attempted(item) and (item['logical_key'] != logical or change != 'unchanged'):
                    issues.append('이미 등록을 시도한 날짜·차시입니다. 쿨메신저 예약 확인·취소 전 자동 재생성하지 않습니다.')
            existing = self._get_job(job_id) if previous and previous['state'] == 'active' else None
            if previous and previous['state'] == 'active' and not existing:
                issues.append('삭제한 초안을 자동으로 다시 만들지 않습니다. 기존 업무 기록을 확인하세요.')
            if existing and _hash(_content(existing)) != previous['render_hash']:
                if change == 'changed':
                    issues.append('개별 수정한 초안과 원본 변경이 겹칩니다. 수동 대조가 필요합니다.')
                else:
                    job = clean_job(existing)  # Keep the user's individual edits.
            candidate = dict(summary, job=job, recipient_names=names, occurrence_keys=occurrence_keys,
                             change=change, issues=list(dict.fromkeys(issues)), render_hash=render_hash,
                             baseline_hash=previous['render_hash'] if previous else None,
                             current_job_hash=_hash(_content(existing)) if existing else None)
            candidates.append(candidate)
            if issues:
                blocked.append(dict(summary, reason='\n'.join(candidate['issues']), code='candidate_blocked',
                                    occurrence_keys=occurrence_keys))
        for logical, item in old.items():
            if item['state'] != 'active' or logical in new_keys:
                continue
            summary = {'logical_key': logical, 'day': item['day'], 'job_id': item['job_id'],
                       'source_rows': json.loads(item['payload']).get('source_rows', []),
                       'baseline_hash': item['render_hash'],
                       'current_job_hash': _hash(_content(self._get_job(item['job_id']))) if self._get_job(item['job_id']) else None}
            diff['deleted'].append(summary)
            if self._attempted(item):
                blocked.append(dict(summary, reason='원본에서 사라진 업무에 등록 시도가 있습니다. 실제 예약은 자동 취소하지 않습니다.',
                                    code='attempted_deleted'))
        preview_id = uuid.uuid4().hex
        payload = {'preview_id': preview_id, 'recipe': cfg, 'snapshot': snap, 'family': family,
                   'period': {'from': start.isoformat(), 'to': end.isoformat()}, 'candidates': candidates,
                   'blocked': blocked, 'unmatched_names': sorted(unmatched), 'diff': diff,
                   'profile_hash': _hash(profile),
                   'stats': {'message_count': len(groups), 'occurrence_count': len(records),
                             'ready_count': sum(not item['issues'] for item in candidates),
                             'blocked_count': len(blocked), 'excluded_count': excluded},
                   'can_commit': not blocked and bool(candidates or diff['deleted'])}
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO workflow_previews VALUES (?,?,NULL,?)', (preview_id, _json(payload), stamp()))
        return payload

    def commit(self, preview_id):
        with self.store.lock:
            row = self.store.db.execute('SELECT payload,result FROM workflow_previews WHERE id=?', (preview_id,)).fetchone()
            if not row:
                raise ValueError('자료 미리보기를 찾을 수 없습니다. 다시 읽어주세요.')
            if row['result']:
                return json.loads(row['result'])
            preview = json.loads(row['payload'])
            if not preview['can_commit']:
                raise ValueError('미리보기의 날짜·문구·계정 연결 오류를 먼저 해결하세요.')
            if _hash(self.store.setting('profile', {'contacts': {}})) != preview['profile_hash']:
                raise ValueError('계정 연결이 미리보기 이후 바뀌었습니다. 다시 검토하세요.')
            for candidate in preview['candidates']:
                problems = validate_job(candidate['job'])
                if problems:
                    raise ValueError('\n'.join(problems))
                current = self.store.db.execute('SELECT * FROM workflow_items WHERE logical_key=?',
                                                 (candidate['logical_key'],)).fetchone()
                if candidate['change'] == 'added' and current and current['state'] == 'active':
                    existing = self._get_job(current['job_id'])
                    if existing and current['render_hash'] == candidate['render_hash']:
                        # Another matching preview was committed first. Reuse
                        # its draft rather than creating a second copy.
                        candidate['job'] = clean_job(existing)
                        candidate['job_id'] = existing['id']
                        candidate['change'] = 'unchanged'
                    else:
                        raise ValueError('미리보기 이후 같은 업무가 달라졌습니다. 다시 검토하세요.')
                elif candidate['change'] != 'added':
                    existing = self._get_job(candidate['job']['id'])
                    if (not current or current['state'] != 'active'
                            or current['render_hash'] != candidate['baseline_hash']
                            or not existing or _hash(_content(existing)) != candidate['current_job_hash']):
                        raise ValueError('미리보기 이후 초안이 변경됐습니다. 다시 검토하세요.')
            deleting_keys = {item['logical_key'] for item in preview['diff']['deleted']}
            active_items = self.store.db.execute("SELECT * FROM workflow_items WHERE family=? AND state='active'",
                                                  (preview['family'],)).fetchall()
            for candidate in preview['candidates']:
                for current in active_items:
                    if (current['logical_key'] != candidate['logical_key'] and current['logical_key'] not in deleting_keys
                            and set(candidate['occurrence_keys']).intersection(json.loads(current['occurrence_keys']))):
                        raise ValueError('미리보기 이후 같은 날짜·차시의 다른 묶음이 생겼습니다. 다시 검토하세요.')
            baseline = {item['logical_key']: item for item in preview['diff']['changed'] + preview['diff']['deleted']}
            for item in baseline.values():
                stored = self.store.db.execute('SELECT * FROM workflow_items WHERE logical_key=?', (item['logical_key'],)).fetchone()
                if stored and self._attempted(dict(stored)):
                    raise ValueError('미리보기 이후 예약 등록을 시도한 업무가 있습니다. 다시 검토하세요.')
                if item['logical_key'] in deleting_keys:
                    existing = self._get_job(item['job_id'])
                    if (not stored or stored['render_hash'] != item['baseline_hash']
                            or (existing and _hash(_content(existing)) != item['current_job_hash'])):
                        raise ValueError('미리보기 이후 삭제할 초안이 바뀌었습니다. 다시 검토하세요.')
            # Everything below shares the same transaction, including the core
            # save_batch savepoint. No UI or external side effect occurs here.
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                deletes = preview['diff']['deleted']
                remove_ids = [item['job_id'] for item in deletes if self._get_job(item['job_id'])]
                if remove_ids:
                    for job_id in remove_ids:
                        current = self.store.get(job_id)
                        if current['status'] in LOCKED | {'preparing'} or self.store.has_attempt(signature(current)):
                            raise ValueError('등록 또는 입력을 시도한 메시지는 자료 갱신으로 삭제하지 않습니다.')
                    self.store.db.executemany('DELETE FROM jobs WHERE id=?', [(job_id,) for job_id in remove_ids])
                changed_jobs = [item['job'] for item in preview['candidates'] if item['change'] != 'unchanged']
                if changed_jobs:
                    self.store.save_batch(changed_jobs)
                snap = preview['snapshot']
                saved_snapshot = self.store.db.execute('SELECT value_hash FROM workflow_snapshots WHERE id=?', (snap['id'],)).fetchone()
                if saved_snapshot and saved_snapshot['value_hash'] != snap['value_hash']:
                    raise ValueError('자료 스냅샷 ID에 다른 값이 있습니다. 다시 읽어주세요.')
                self.store.db.execute('INSERT OR IGNORE INTO workflow_snapshots VALUES (?,?,?,?,?)',
                                      (snap['id'], snap['source_key'], snap['value_hash'], _json(snap), stamp()))
                for candidate in preview['candidates']:
                    if candidate['change'] == 'unchanged':
                        continue
                    self.store.db.execute('INSERT OR REPLACE INTO workflow_items VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (candidate['logical_key'], preview['family'], candidate['day'], candidate['job']['id'],
                         _json(candidate['occurrence_keys']), candidate['render_hash'], _json(candidate),
                         snap['id'], preview['recipe']['version'], 'active'))
                for item in deletes:
                    self.store.db.execute("UPDATE workflow_items SET state='deleted',snapshot_id=? WHERE logical_key=?",
                                          (snap['id'], item['logical_key']))
                result = {'preview_id': preview_id, 'saved': sum(item['change'] == 'added' for item in preview['candidates']),
                          'updated': sum(item['change'] == 'changed' for item in preview['candidates']), 'deleted': len(deletes),
                          'unchanged': sum(item['change'] == 'unchanged' for item in preview['candidates']),
                          'job_ids': [item['job']['id'] for item in preview['candidates']]}
                self.store.db.execute('UPDATE workflow_previews SET result=? WHERE id=?', (_json(result), preview_id))
                self.store.db.commit()
                return result
            except Exception:
                self.store.db.rollback()
                raise
