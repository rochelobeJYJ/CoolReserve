"""Run synthetic regressions with isolated temporary storage; never open a browser."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
scratch = ROOT / '.test-temp'
scratch.mkdir(exist_ok=True)
tempfile.tempdir = str(scratch)
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
report = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
          'skipped': len(result.skipped), 'actual_messenger_operated': False,
          'problems': [{'test': test.id(), 'trace': trace} for test, trace in result.failures + result.errors]}
(ROOT / 'test-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(stream.getvalue() if not result.wasSuccessful() else json.dumps({k: v for k, v in report.items() if k != 'problems'}))
sys.exit(not result.wasSuccessful())
