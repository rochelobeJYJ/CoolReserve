"""Check installation without reading an address book or opening any windows."""
import importlib.metadata
import json
import sys


def check():
    problems = []
    if sys.platform != 'win32':
        problems.append('Windows is required for messenger registration.')
    if sys.version_info[:2] not in ((3, 13), (3, 14)) or sys.maxsize <= 2**32:
        problems.append('64-bit Python 3.13 or 3.14 is required.')
    for name, version in {'pywinauto': '0.6.9', 'comtypes': '1.4.17', 'pywin32': '312', 'six': '1.17.0'}.items():
        try:
            if importlib.metadata.version(name) != version:
                problems.append(name + ': run INSTALL.cmd to install the pinned version.')
        except importlib.metadata.PackageNotFoundError:
            problems.append(name + ': missing; run INSTALL.cmd.')
    return {'ready': not problems, 'problems': problems, 'messenger_operated': False}


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(not result['ready'])
