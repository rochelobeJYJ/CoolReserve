"""Audit an explicit public file set. Findings contain locations, never matched values."""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import stat
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CONFIG = 'public-release.json'
RECEIPT = 'RELEASE-MANIFEST.json'
MAX_FILE = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
TEXT_SUFFIXES = {'.py', '.js', '.cjs', '.css', '.html', '.svg', '.md', '.json', '.csv', '.txt', '.cmd', '.ps1'}
FORBIDDEN_PARTS = {'.git', '.qa-data', '.test-temp', '.offline-data', '.venv', '__pycache__',
                   'output', 'outputs', 'dumps', 'runtime', 'node_modules', 'ui-proposal', 'research'}
FORBIDDEN_NAMES = {'session.json', '.gentoo_inspection.json', 'windows_v1_original.txt',
                   'source-example.json', 'windows_test_result.txt'}


class PublicAuditError(ValueError):
    """Carries no original value, external path, or exception text."""


def safe_relative(name: str) -> bool:
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        return False
    return not PurePosixPath(name).is_absolute() and all(p not in ('', '.', '..') for p in name.split('/'))


def forbidden(name: str) -> bool:
    parts = PurePosixPath(name).parts
    lower = [p.lower() for p in parts]
    base = lower[-1]
    return (any(p in FORBIDDEN_PARTS for p in lower)
            or any(p.startswith('.') and p != '.gitignore' for p in parts)
            or base in FORBIDDEN_NAMES or base.startswith(('handoff', '_transfer', '.env'))
            or PurePosixPath(base).suffix in {'.db', '.sqlite', '.sqlite3', '.log', '.png', '.jpg', '.jpeg', '.gif', '.exe', '.dll', '.pyc', '.pfx', '.pem', '.key'}
            or (PurePosixPath(base).suffix == '.json' and any(s in base for s in ('oauth', 'credential', 'token', 'session'))))


def load_allowlist(root: Path, config: str = CONFIG) -> list[str]:
    try:
        if not safe_relative(config):
            raise ValueError
        obj = json.loads(checked_file(root, config).read_text(encoding='utf-8-sig'))
        files = obj['files']
        if (obj.get('schema_version') != 1 or not isinstance(files, list) or not files
                or any(not safe_relative(n) or forbidden(n) or n == RECEIPT for n in files)
                or len({n.casefold() for n in files}) != len(files)):
            raise ValueError
        return sorted(files)
    except (OSError, KeyError, TypeError, ValueError):
        raise PublicAuditError('invalid_public_manifest') from None


def load_deny_file(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        values = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(values, list) or any(not isinstance(v, str) or len(v.strip()) < 2 for v in values):
            raise ValueError
        return [unicodedata.normalize('NFKC', v.strip()) for v in values]
    except (OSError, ValueError, TypeError):
        raise PublicAuditError('invalid_private_deny_file') from None


def decoded_text(text: str) -> str:
    text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m[1], 16)), text)
    return unicodedata.normalize('NFKC', html.unescape(text))


def text_findings(text: str, deny: list[str], license_text: bool = False) -> list[dict]:
    decoded = decoded_text(text)
    patterns = {
        'private_sheet_literal': r'(?:docs\.google\.com/spreadsheets/d/|["\'](?:spreadsheet_id|sheet_id)["\']\s*:\s*["\'])(?!FIXTURE|fixture|SYNTHETIC|synthetic)[A-Za-z0-9_-]{20,}',
        'private_absolute_path': r'(?:[A-Za-z]:[\\/]+(?:Users|OneDrive|chatgpt_work|codex_work)[\\/]|/' + r'Users/[^/\s]+/)',
        'personal_account_literal': r'(?<![가-힣])(?!(?:합성|가상|예시|테스트|샘플))[가-힣]{2,8}\(\d{2,}\)',
        'secret_literal': r'(?:ghp_|github_pat_|sk-proj-|sk-|xox[baprs]-)[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'oauth_literal': r'[A-Za-z0-9_-]{16,}\.apps\.googleusercontent\.com|["\'](?:client_secret|refresh_token|access_token)["\']\s*:\s*["\'][A-Za-z0-9_./-]{20,}',
    }
    if not license_text:
        patterns['personal_email_literal'] = r'\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b|localhost\b|test\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
        patterns['school_literal'] = r'[가-힣]{2,}(?:고등학교|중학교|초등학교)'
    findings = []
    for category, pattern in patterns.items():
        for match in re.finditer(pattern, decoded):
            findings.append({'type': category, 'line': decoded.count('\n', 0, match.start()) + 1})
    for value in deny:
        offset = decoded.find(value)
        if offset >= 0:
            findings.append({'type': 'private_deny_match', 'line': decoded.count('\n', 0, offset) + 1})
    return list({(f['type'], f['line']): f for f in findings}.values())


def inspect_bytes(name: str, data: bytes, deny: list[str]) -> list[dict]:
    if len(data) > MAX_FILE:
        return [{'type': 'file_too_large'}]
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == '.xlsx':
        issues = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if archive.comment:
                    issues.append({'type': 'archive_metadata_not_allowed'})
                if len(infos) > 1000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                    return [{'type': 'archive_limits'}]
                seen = set()
                for ordinal, info in enumerate(infos, 1):
                    entry = info.filename
                    location = {'archive_entry': ordinal}
                    if (not safe_relative(entry) or entry.casefold() in seen or info.flag_bits & 1 or info.comment or info.extra
                            or stat.S_ISLNK(info.external_attr >> 16)
                            or (PurePosixPath(entry).suffix not in {'.xml', '.rels'} and PurePosixPath(entry).name != '.rels')
                            or any(p in entry.lower() for p in ('externallinks/', 'embeddings/', 'vbaproject', 'customxml/'))):
                        issues.append(dict(location, type='unsafe_archive_entry'))
                        continue
                    seen.add(entry.casefold())
                    issues.extend(dict(location, **f) for f in text_findings(entry, deny))
                    raw = archive.read(info)
                    value = raw.decode('utf-8-sig')
                    if '<!DOCTYPE' in value or '<!ENTITY' in value:
                        issues.append(dict(location, type='xml_dtd_not_allowed'))
                        continue
                    xml = ET.fromstring(raw)
                    semantic = '\n'.join(''.join(xml.itertext()).splitlines())
                    issues.extend(dict(location, **f) for f in text_findings(value, deny))
                    issues.extend(dict(location, **f) for f in text_findings(semantic, deny))
        except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError, ET.ParseError):
            issues.append({'type': 'invalid_workbook_archive'})
        return issues
    if suffix not in TEXT_SUFFIXES and PurePosixPath(name).name not in {'LICENSE', '.gitignore'}:
        return [{'type': 'unsupported_file_type'}]
    try:
        text = data.decode('utf-8-sig')
        if '\x00' in text:
            return [{'type': 'binary_text_file'}]
        return text_findings(text, deny, license_text=name.startswith('third-party/'))
    except UnicodeError:
        return [{'type': 'invalid_text_encoding'}]


def checked_file(root: Path, name: str) -> Path:
    root = root.resolve()
    path = root / name
    current = root
    for part in PurePosixPath(name).parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise PublicAuditError('linked_path')
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise PublicAuditError('missing_file')
    return path


def file_receipt(root: Path, files: list[str]) -> list[dict]:
    result = []
    for name in files:
        data = checked_file(root, name).read_bytes()
        result.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return result


def audit(root: Path, *, config: str = CONFIG, staging: bool = False, deny: list[str] | None = None) -> dict:
    deny = deny or []
    files = load_allowlist(root, config)
    problems = []
    for name in files:
        try:
            path = checked_file(root, name)
            issues = inspect_bytes(name, path.read_bytes(), deny)
        except (OSError, PublicAuditError) as exc:
            issues = [{'type': str(exc) if isinstance(exc, PublicAuditError) else 'unreadable_file'}]
        problems.extend(dict(file=name, **issue) for issue in issues)
    if staging:
        expected = set(files) | {RECEIPT}
        for path in root.rglob('*'):
            name = path.relative_to(root).as_posix()
            if forbidden(name):
                problems.append({'file': '<unlisted>', 'type': 'forbidden_staging_path'})
            elif path.is_symlink() or path.is_junction():
                problems.append({'file': '<unlisted>', 'type': 'linked_staging_path'})
            elif path.is_file() and name not in expected:
                problems.append({'file': '<unlisted>', 'type': 'unlisted_staging_file'})
        try:
            receipt = json.loads(checked_file(root, RECEIPT).read_text(encoding='utf-8'))
            if receipt != {'schema_version': 1, 'files': file_receipt(root, files)}:
                problems.append({'file': RECEIPT, 'type': 'fingerprint_mismatch'})
        except (OSError, ValueError, PublicAuditError):
            problems.append({'file': RECEIPT, 'type': 'invalid_fingerprint_manifest'})
    return {'passed': not problems, 'mode': 'staging' if staging else 'source_allowlist',
            'checked_files': len(files), 'problems': problems, 'values_omitted': True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--config', default=CONFIG)
    parser.add_argument('--staging', action='store_true')
    parser.add_argument('--deny-file', type=Path, help='Private local JSON array; never copied or printed.')
    args = parser.parse_args()
    try:
        result = audit(args.root, config=args.config, staging=args.staging, deny=load_deny_file(args.deny_file))
    except PublicAuditError as exc:
        result = {'passed': False, 'problems': [{'type': str(exc)}], 'values_omitted': True}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(not result['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
