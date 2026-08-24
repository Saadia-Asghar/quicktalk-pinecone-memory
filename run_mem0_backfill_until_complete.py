"""Keep the resumable all-organization Mem0 backfill running to completion."""

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROGRESS = ROOT / "mem0_all_org_progress.txt"
TOTAL = 16588


def current() -> int:
    try:
        return int(PROGRESS.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


while current() < TOTAL:
    result = subprocess.run(
        [sys.executable, str(ROOT / "import_all_org_mem0.py")], cwd=ROOT, check=False
    )
    if current() >= TOTAL:
        break
    # Back off after rate limits or transient network/backend failures, then resume.
    time.sleep(120 if result.returncode else 10)
