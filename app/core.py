from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))
MAX_JOBS = 500
STATES = {'draft': '대기', 'preparing': '입력 중', 'prepared': '입력 확인',
          'submitting': '등록 중', 'needs_review': '결과 확인 필요',
          'confirmed': '예약 등록 확인', 'uncertain': '등록 여부 불명', 'failed': '중단'}
LOCKED = {'submitting', 'needs_review', 'confirmed', 'uncertain'}
PAST_ARCHIVABLE = {'confirmed', 'needs_review', 'draft', 'failed'}
DEFAULT_RESULT_CONFIRMATION_MODE = 'manual'
RESULT_CONFIRMATION_MODES = ('manual', 'automatic')
HEADERS = ['예약일시', '받는사람', '제목', '내용', '첨부파일']


def validate_result_confirmation_mode(value):
    if not isinstance(value, str) or value not in RESULT_CONFIRMATION_MODES:
        raise ValueError('결과 확인 방식은 manual 또는 automatic이어야 합니다.')
    return value


def now() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def stamp() -> str:
    return now().isoformat(timespec='seconds')


def normalize(text: Any) -> str:
    return str(text or '').replace('\r\n', '\n').replace('\r', '\n').strip()


def parse_time(value: Any) -> datetime:
    text = str(value or '').strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt).replace(second=0, microsecond=0)
        except ValueError:
            pass
    raise ValueError('예약일시는 YYYY-MM-DD HH:MM 형식이어야 합니다.')


def add_months(dt: datetime, count: int) -> datetime:
    m = dt.month - 1 + count
    y = dt.year + m // 12
    month = m % 12 + 1
    return dt.replace(year=y, month=month, day=min(dt.day, calendar.monthrange(y, month)[1]))


def parse_paths(value: Any) -> list[str]:
    items = value if isinstance(value, list) else re.split(r'[;\n]', str(value or ''))
    return list(dict.fromkeys(str(x).strip().strip('"') for x in items if str(x).strip()))


def clean_job(raw: dict[str, Any]) -> dict[str, Any]:
    scheduled = parse_time(raw.get('scheduled', raw.get('예약일시'))).strftime('%Y-%m-%d %H:%M')
    job = {
        'id': str(raw.get('id') or uuid.uuid4().hex),
        'scheduled': scheduled,
        'recipient': normalize(raw.get('recipient', raw.get('받는사람', raw.get('받는 사람', '')))),
        'title': normalize(raw.get('title', raw.get('제목', ''))),
        'body': normalize(raw.get('body', raw.get('내용', ''))),
        'attachments': parse_paths(raw.get('attachments', raw.get('첨부파일', ''))),
    }
    if 'recipient_ids' in raw:
        ids = raw['recipient_ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(x, str) or not x.strip() for x in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('수신자 연결은 중복 없는 1~100개 항목이어야 합니다.')
        job['recipient_ids'] = list(ids)
        bindings = raw.get('recipient_bindings', {})
        if not isinstance(bindings, dict) or any(x not in ids for x in bindings):
            raise ValueError('수신자 확인 정보가 잘못되었습니다.')
        for evidence in bindings.values():
            if not isinstance(evidence, dict) or not isinstance(evidence.get('selector'), dict) or not normalize(evidence.get('expected')):
                raise ValueError('수신자 확인 정보에 표시 이름과 화면 연결이 필요합니다.')
        job['recipient_bindings'] = json.loads(json.dumps(bindings, ensure_ascii=False))
    return job


def recipient_ids(job: dict) -> list[str]:
    return job.get('recipient_ids', [job.get('recipient', '')])


def signature(job: dict, identity: str = '') -> str:
    # Content, not row IDs, is the idempotency key. Preserve body whitespace.
    payload = {k: job.get(k, '') for k in ('scheduled', 'recipient', 'title', 'body', 'attachments')}
    payload['identity'] = identity
    if 'recipient_ids' in job:
        # Display labels may change; account bindings and the recipient set may not.
        payload.pop('recipient')
        payload['recipient_ids'] = sorted(job['recipient_ids'])
        payload['recipient_bindings'] = job.get('recipient_bindings', {})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_info(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(f'첨부파일은 전체 경로로 입력하세요: {path}')
    if not p.is_file():
        raise ValueError(f'첨부파일을 찾을 수 없습니다: {path}')
    stat = p.stat()
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return {'path': str(p), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': h.hexdigest()}


def validate_job(job: dict, current: datetime | None = None, check_files: bool = True) -> list[str]:
    errors: list[str] = []
    current = current or now()
    try:
        dt = parse_time(job.get('scheduled'))
        if dt < current + timedelta(minutes=10):
            errors.append('예약시간은 지금부터 10분 이상 뒤로 지정하세요. (5분 제한에 여유를 둡니다.)')
        if dt > add_months(current, 3):
            errors.append('예약시간이 현재부터 3개월을 넘습니다.')
    except ValueError as e:
        errors.append(str(e))
    if not normalize(job.get('recipient')):
        errors.append('받는 사람 또는 수신자 프리셋을 입력하세요.')
    if not normalize(job.get('body')):
        errors.append('메시지 내용을 입력하세요.')
    if len(job.get('title', '')) > 200:
        errors.append('제목은 이 프로그램에서 200자까지 허용합니다.')
    if len(job.get('body', '')) > 30000:
        errors.append('본문은 이 프로그램에서 30,000자까지 허용합니다.')
    for field in ('title', 'recipient', 'body'):
        if '\x00' in job.get(field, ''):
            errors.append(f'{field}: 널 문자는 사용할 수 없습니다.')
        if any(0xD800 <= ord(c) <= 0xDFFF for c in job.get(field, '')):
            errors.append(f'{field}: 잘못된 유니코드 문자가 있습니다. 다시 입력하세요.')
    if '\t' in job.get('title', '') or '\t' in job.get('body', ''):
        errors.append('제목·본문의 탭 문자는 공백으로 바꿔주세요. 화면 입력에서 탭을 지원하지 않습니다.')
    paths = job.get('attachments', [])
    if len(paths) > 10:
        errors.append('첨부파일은 이 프로그램에서 한 메시지당 10개까지 허용합니다.')
    if len({Path(p).name.casefold() for p in paths}) != len(paths):
        errors.append('같은 이름의 첨부파일은 한 메시지에 함께 넣을 수 없습니다.')
    if check_files:
        for p in paths:
            try:
                if not Path(p).is_absolute() or not Path(p).is_file():
                    errors.append(f'첨부파일 전체 경로를 확인하세요: {p}')
            except (OSError, ValueError):
                errors.append(f'잘못된 첨부파일 경로: {p}')
    return errors


def expand_repeat(base: dict, end: str, weekdays: list[int], excluded: list[str],
                  current: datetime | None = None) -> list[dict]:
    dt = parse_time(base['scheduled'])
    until = date.fromisoformat(end)
    if until < dt.date():
        raise ValueError('반복 종료일이 시작일보다 빠릅니다.')
    if until > add_months(current or now(), 3).date():
        raise ValueError('반복 종료일은 현재부터 3개월 이내로 지정하세요.')
    if not weekdays or any(type(x) is not int or x < 0 or x > 6 for x in weekdays):
        raise ValueError('반복 요일을 선택하세요.')
    skipped = {date.fromisoformat(s) for s in excluded if s.strip()}
    result = []
    day = dt.date()
    while day <= until:
        if day.weekday() in weekdays and day not in skipped:
            job = dict(base)
            job['id'] = uuid.uuid4().hex
            job['scheduled'] = datetime.combine(day, dt.time()).strftime('%Y-%m-%d %H:%M')
            result.append(clean_job(job))
        day += timedelta(days=1)
    if not result:
        raise ValueError('조건에 해당하는 날짜가 없습니다.')
    return result


def safe_csv(value: Any) -> str:
    s = str(value or '')
    if s.lstrip().startswith(('=', '+', '-', '@')) or s.startswith(('\t', '\r')):
        return "'" + s
    return s


def export_csv(jobs: list[dict], include_status: bool = False) -> bytes:
    stream = io.StringIO(newline='')
    out = csv.writer(stream)
    explicit = any('recipient_ids' in j for j in jobs)
    out.writerow(HEADERS + (['수신자ID', '수신자확인정보'] if explicit else []) + (['상태', '메모'] if include_status else []))
    for j in jobs:
        cells = [j['scheduled'], j['recipient'], j['title'], j['body'], ';'.join(j['attachments'])]
        if explicit:
            cells += [json.dumps(j.get('recipient_ids', []), ensure_ascii=False),
                      json.dumps(j.get('recipient_bindings', {}), ensure_ascii=False)]
        if include_status:
            cells += [STATES.get(j.get('status'), j.get('status', '')), j.get('note', '')]
        out.writerow([safe_csv(v) for v in cells])
    return stream.getvalue().encode('utf-8-sig')


class Store:
    """Single-process SQLite store. Attempt fingerprints survive draft deletion."""
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / 'reservations.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL,
          fingerprint TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(fingerprint TEXT PRIMARY KEY, job_id TEXT NOT NULL,
          status TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_archives(job_id TEXT PRIMARY KEY, archived_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
          job_id TEXT, kind TEXT NOT NULL, message TEXT NOT NULL);
        ''')
        with self.db:
            self.db.execute("UPDATE jobs SET status='uncertain',note='실행이 중단되었습니다. 쿨메신저에서 등록 여부를 먼저 확인하세요.' WHERE status='submitting'")
            self.db.execute("UPDATE jobs SET status='failed',note='입력 중 프로그램이 종료되었습니다. 미전송 작성창을 확인하세요.' WHERE status='preparing'")
            self.db.execute("UPDATE attempts SET status='uncertain' WHERE status='submitting'")

    def close(self):
        self.db.close()

    def setting(self, key: str, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set_setting(self, key: str, value: Any):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def list_jobs(self, include_archived: bool = True) -> list[dict]:
        # Internal duplicate and workflow checks must keep seeing all history.
        # Only the user-facing default list requests include_archived=False.
        condition = '' if include_archived else ' WHERE NOT EXISTS (SELECT 1 FROM job_archives a WHERE a.job_id=jobs.id)'
        with self.lock:
            rows = self.db.execute('SELECT * FROM jobs' + condition + ' ORDER BY json_extract(payload,\'$.scheduled\'),updated').fetchall()
        return [dict(json.loads(r['payload']), status=r['status'], note=r['note'], updated=r['updated']) for r in rows]

    def list_archived(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute('SELECT j.*,a.archived_at FROM jobs j JOIN job_archives a ON a.job_id=j.id '
                                   'ORDER BY a.archived_at DESC,json_extract(j.payload,\'$.scheduled\') DESC').fetchall()
        return [dict(json.loads(r['payload']), status=r['status'], note=r['note'], updated=r['updated'],
                     archived_at=r['archived_at']) for r in rows]

    @staticmethod
    def _archive_ids(ids):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_JOBS
                or any(not isinstance(jid, str) or not jid.strip() for jid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('메시지를 중복 없이 1~500개 선택하세요.')
        return ids

    @staticmethod
    def _can_archive(job, today):
        try:
            return job['status'] in PAST_ARCHIVABLE and parse_time(job['scheduled']).date() < today
        except (ValueError, KeyError):
            return False

    def past_cleanup_count(self) -> int:
        today = now().date()
        return sum(self._can_archive(job, today) for job in self.list_jobs(include_archived=False))

    def archive_past(self, ids=None) -> dict:
        if ids is not None:
            self._archive_ids(ids)
        with self.lock, self.db:
            today = now().date()
            if ids is None:
                jobs = [job for job in self.list_jobs(include_archived=False) if self._can_archive(job, today)]
            else:
                jobs = [self.get(jid) for jid in ids]
                if any(not self._can_archive(job, today) for job in jobs):
                    raise ValueError('오늘 이전의 완료·등록 요청 완료·대기·중단 메시지만 정리할 수 있습니다. 입력 중이거나 등록 여부가 불명확한 메시지는 남겨둡니다.')
            archived = {row['job_id'] for row in self.db.execute('SELECT job_id FROM job_archives')}
            selected = [job['id'] for job in jobs if job['id'] not in archived]
            archived_at = stamp()
            self.db.executemany('INSERT INTO job_archives(job_id,archived_at) VALUES (?,?)',
                                [(jid, archived_at) for jid in selected])
        return {'archived': len(selected), 'job_ids': selected}

    def restore_archived(self, ids) -> dict:
        self._archive_ids(ids)
        with self.lock, self.db:
            for jid in ids:
                self.get(jid)
            archived = {row['job_id'] for row in self.db.execute('SELECT job_id FROM job_archives')}
            selected = [jid for jid in ids if jid in archived]
            if len(self.list_jobs(include_archived=False)) + len(selected) > MAX_JOBS:
                raise ValueError('복원 후 기본 목록은 500건 이하여야 합니다. 지난 메시지를 먼저 정리하세요.')
            self.db.executemany('DELETE FROM job_archives WHERE job_id=?', [(jid,) for jid in selected])
        return {'restored': len(selected), 'job_ids': selected}

    def get(self, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('메시지를 찾을 수 없습니다.')
        return dict(json.loads(row['payload']), status=row['status'], note=row['note'], updated=row['updated'])

    def save(self, raw: dict) -> dict:
        return self.save_batch([raw])[0]

    def save_batch(self, raw_jobs: list[dict]) -> list[dict]:
        if not isinstance(raw_jobs, list) or len(raw_jobs) > MAX_JOBS:
            raise ValueError(f'메시지는 {MAX_JOBS}건 이하여야 합니다.')
        jobs = [clean_job(raw) for raw in raw_jobs]
        if len({j['id'] for j in jobs}) != len(jobs):
            raise ValueError('중복된 메시지 ID가 있습니다.')
        fingerprints = [signature(j) for j in jobs]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError('수신자·예약시간·제목·내용·첨부가 같은 메시지가 있습니다.')
        with self.lock:
            # A savepoint is atomic on its own and never commits a caller's transaction.
            savepoint = 'batch_' + uuid.uuid4().hex
            self.db.execute('SAVEPOINT ' + savepoint)
            try:
                existing = {r['id']: r for r in self.db.execute('SELECT id,status,fingerprint FROM jobs')}
                archived = {r['job_id'] for r in self.db.execute('SELECT job_id FROM job_archives')}
                visible = set(existing) - archived
                if len(visible | {j['id'] for j in jobs}) > MAX_JOBS:
                    raise ValueError(f'기본 목록은 {MAX_JOBS}건 이하여야 합니다. 지난 메시지를 먼저 정리하세요.')
                changing = {j['id'] for j in jobs}
                for j, fp in zip(jobs, fingerprints):
                    old = existing.get(j['id'])
                    if old and old['status'] in LOCKED | {'preparing'}:
                        raise ValueError('등록을 시도한 메시지는 수정할 수 없습니다. 먼저 쿨메신저에서 결과를 확인하세요.')
                    if any(r['fingerprint'] == fp and r['id'] not in changing for r in existing.values()):
                        raise ValueError('수신자·예약시간·제목·내용·첨부가 같은 메시지가 이미 있습니다.')
                    if self.has_attempt(fp):
                        raise ValueError('같은 메시지의 등록 시도 기록이 있습니다. 자동 재등록하지 않습니다.')
                for j, fp in zip(jobs, fingerprints):
                    self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)',
                                    (j['id'], json.dumps(j, ensure_ascii=False), fp, 'draft', '', stamp()))
                    self.db.execute('DELETE FROM job_archives WHERE job_id=?', (j['id'],))
                self.db.execute('RELEASE SAVEPOINT ' + savepoint)
            except Exception:
                self.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                self.db.execute('RELEASE SAVEPOINT ' + savepoint)
                raise
        return jobs

    def remove(self, ids: list[str]):
        with self.lock, self.db:
            for jid in ids:
                j = self.get(jid)
                if j['status'] in LOCKED | {'preparing'}:
                    raise ValueError('등록 시도 기록은 삭제하지 않습니다. 이 앱의 삭제는 쿨메신저 예약취소 기능이 아닙니다.')
            for jid in ids:
                self.db.execute('DELETE FROM jobs WHERE id=?', (jid,))
                self.db.execute('DELETE FROM job_archives WHERE job_id=?', (jid,))

    def status(self, job_id: str, status: str, note: str = ''):
        if status not in STATES:
            raise ValueError('잘못된 상태입니다.')
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET status=?,note=?,updated=? WHERE id=?', (status, note, stamp(), job_id))
            self.db.execute('INSERT INTO events(at,job_id,kind,message) VALUES (?,?,?,?)', (stamp(), job_id, status, note))

    def has_attempt(self, fp: str) -> bool:
        with self.lock:
            return self.db.execute('SELECT 1 FROM attempts WHERE fingerprint=?', (fp,)).fetchone() is not None

    def begin_submit(self, job: dict):
        # Persist before clicking. A crash can never silently create a retry.
        with self.lock, self.db:
            if self.has_attempt(signature(job)):
                raise ValueError('등록 시도 기록이 있어 중복 전송을 차단했습니다.')
            self.db.execute('INSERT INTO attempts VALUES (?,?,?,?)', (signature(job), job['id'], 'submitting', stamp()))
            self.db.execute('UPDATE jobs SET status=?,updated=? WHERE id=?', ('submitting', stamp(), job['id']))

    def finish_submit(self, job: dict, state: str, note: str):
        with self.lock, self.db:
            self.db.execute('UPDATE attempts SET status=?,updated=? WHERE fingerprint=?', (state, stamp(), signature(job)))
            self.status(job['id'], state, note)

    def review(self, job_id: str, note: str):
        j = self.get(job_id)
        if j['status'] not in {'needs_review', 'uncertain'}:
            raise ValueError('결과 확인 대기 또는 등록 여부 불명인 메시지만 확인 처리할 수 있습니다.')
        if len(note.strip()) < 5:
            raise ValueError('쿨메신저에서 확인한 수신자와 예약시각을 메모에 남겨주세요.')
        self.finish_submit(j, 'confirmed', '쿨메신저 예약 목록에서 대조 완료했습니다.')

    def events(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 300')]
