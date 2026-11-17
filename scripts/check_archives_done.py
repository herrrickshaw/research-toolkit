#!/usr/bin/env python3
"""Gate G6: both archive-extraction passes reached their completion marker."""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = [
    os.path.join(REPO_ROOT, "gdrive_archive_run.log"),
]

all_done = True
for path in LOGS:
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        print(f"LOG_NOT_FOUND: {path}")
        all_done = False
        continue
    if "=== ARCHIVES DONE:" in content:
        line = [l for l in content.splitlines() if "=== ARCHIVES DONE:" in l][-1]
        print(f"{path}: {line}")
    else:
        tail = "\n".join(content.splitlines()[-3:])
        print(f"{path}: NOT DONE, last lines:\n{tail}")
        all_done = False

if all_done:
    print("ARCHIVES_DONE")
    sys.exit(0)
else:
    print("ARCHIVES_NOT_DONE")
    sys.exit(1)
