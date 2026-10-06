"""Copy only audited public files into a new staging directory and deterministic ZIP."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import zipfile
from pathlib import Path

try:
    from tools.check_public import (ROOT, CONFIG, RECEIPT, PublicAuditError, audit, checked_file,
                                    file_receipt, inspect_bytes, load_allowlist, load_deny_file)
except ModuleNotFoundError:
    from check_public import (ROOT, CONFIG, RECEIPT, PublicAuditError, audit, checked_file,
                             file_receipt, inspect_bytes, load_allowlist, load_deny_file)


def build(root: Path, destination: Path, *, config: str = CONFIG, deny: list[str] | None = None) -> dict:
    root, destination = root.resolve(), destination.absolute()
    zip_destination = destination.with_name(destination.name + '.zip')
    deny = deny or []
    if any(p.is_symlink() or p.is_junction() for p in [destination, *destination.parents]):
        raise PublicAuditError('linked_destination')
    if destination.exists() or destination.is_symlink() or zip_destination.exists():
        raise PublicAuditError('destination_exists_use_new_directory')
    if destination == root or root.is_relative_to(destination):
        raise PublicAuditError('invalid_destination')
    source_report = audit(root, config=config, deny=deny)
    if not source_report['passed']:
        return source_report
    files = load_allowlist(root, config)
    if config not in files:
        raise PublicAuditError('manifest_not_allowlisted')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='coolreserve-public-', dir=destination.parent) as scratch:
        staging = Path(scratch) / 'stage'
        staging.mkdir()
        for name in files:
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = checked_file(root, name).read_bytes()
            if inspect_bytes(name, data, deny):
                raise PublicAuditError('source_changed_during_build')
            target.write_bytes(data)
        receipt = {'schema_version': 1, 'files': file_receipt(staging, files)}
        (staging / RECEIPT).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        staged_report = audit(staging, config=config, staging=True, deny=deny)
        if not staged_report['passed']:
            return staged_report
        zip_temp = Path(scratch) / 'release.zip'
        with zipfile.ZipFile(zip_temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files + [RECEIPT]):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (staging / name).read_bytes())
        with zipfile.ZipFile(zip_temp) as archive:
            if sorted(archive.namelist()) != sorted(files + [RECEIPT]):
                raise PublicAuditError('zip_inventory_mismatch')
            for name in archive.namelist():
                if archive.read(name) != (staging / name).read_bytes():
                    raise PublicAuditError('zip_content_mismatch')
        if destination.exists() or zip_destination.exists():
            raise PublicAuditError('destination_changed_during_build')
        os.replace(staging, destination)
        os.replace(zip_temp, zip_destination)
    return {'passed': True, 'files': len(files), 'fingerprint_manifest': RECEIPT,
            'zip_verified': True, 'values_omitted': True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'CoolReserve-public')
    parser.add_argument('--config', default=CONFIG)
    parser.add_argument('--deny-file', type=Path)
    args = parser.parse_args()
    try:
        result = build(args.root, args.output, config=args.config, deny=load_deny_file(args.deny_file))
    except (OSError, PublicAuditError) as exc:
        result = {'passed': False, 'problems': [{'type': str(exc) if isinstance(exc, PublicAuditError) else 'release_io_error'}], 'values_omitted': True}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(not result['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
