#!/usr/bin/env python3
"""Gate G6: both archive-extraction passes reached their completion marker."""
import sys

LOGS = [
    "/private/tmp/claude-501/-Users-umashankar/cbbd889a-6c4e-47b9-85de-60aa65a42e93/scratchpad/archive_run.log",
    "/Users/umashankar/research-toolkit/gdrive_archive_run.log",
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
