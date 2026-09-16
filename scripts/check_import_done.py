#!/usr/bin/env python3
"""Gates G4/G5: import script reached its own natural completion marker."""
import argparse
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--log", required=True)
args = parser.parse_args()

try:
    with open(args.log) as f:
        content = f.read()
except FileNotFoundError:
    print(f"LOG_NOT_FOUND: {args.log}")
    sys.exit(1)

if "=== DONE:" in content:
    line = [l for l in content.splitlines() if "=== DONE:" in l][-1]
    print(line)
    print("IMPORT_DONE")
    sys.exit(0)
else:
    tail = "\n".join(content.splitlines()[-5:])
    print(f"IMPORT_NOT_DONE, last lines:\n{tail}")
    sys.exit(1)
