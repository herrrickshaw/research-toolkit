#!/usr/bin/env python3
"""Print top-level Dropbox folders that appeared since the last run.

usage: check_new_folders.py <file with one folder name per line>
First run records a baseline (seen_top_folders.txt) and alerts on nothing.
Folders in ignore_folders.txt are never reported.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
seen_f = os.path.join(HERE, "seen_top_folders.txt")
ignore = {l.strip() for l in open(os.path.join(HERE, "ignore_folders.txt"))
          if l.strip() and not l.startswith("#")}
top = {l.rstrip("\n") for l in open(sys.argv[1]) if l.strip()}
if not os.path.exists(seen_f):
    open(seen_f, "w").write("\n".join(sorted(top)) + "\n")
    print(f"baseline recorded: {len(top)} top-level folders (later runs alert only on new ones)")
else:
    seen = {l.rstrip("\n") for l in open(seen_f)}
    new = sorted(t for t in top - seen if t not in ignore)
    print("NEW TOP-LEVEL FOLDERS since last run:", new if new else "none")
    if new:
        open(seen_f, "a").write("".join(t + "\n" for t in new))
