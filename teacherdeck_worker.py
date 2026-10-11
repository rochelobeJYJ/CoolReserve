# SPDX-License-Identifier: MIT
"""Fixed isolated entrypoint: admit only this distribution's canonical source."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.teacherdeck_worker import main

if __name__ == '__main__':
    try:
        code = main()
    except Exception:
        # Startup failures must not expose ledger paths or dependency traces.
        code = 2
    raise SystemExit(code)
