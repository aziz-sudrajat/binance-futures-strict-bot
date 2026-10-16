#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Push changed bot files to GitHub via the github MCP CLI (one file per call)."""
import json
import subprocess
import sys

OWNER, REPO, BRANCH = "aziz-sudrajat", "binance-futures-strict-bot", "main"
MSG = "Trading-hours gate: LIVE entries only 05:00-18:00 WIB (ketat)"
FILES = ["config.py", "executor.py", "dashboard.py",
         "tests/test_trading_hours.py", "tests/test_from_db.py"]


def gh(args):
    r = subprocess.run(["github"] + args, capture_output=True, text=True,
                       timeout=300)
    return r


def get_sha(path):
    r = gh(["call-read-tool", "--name", "get_file_contents",
            "--arguments-json", json.dumps(
                {"owner": OWNER, "repo": REPO, "path": path})])
    if r.returncode != 0:
        return None
    try:
        txt = json.loads(r.stdout)["result"]["content"][0]["text"]
    except (KeyError, IndexError, ValueError):
        return None
    # "successfully downloaded text file (SHA: <sha>)"
    if "SHA:" in txt:
        return txt.split("SHA:")[1].split(")")[0].strip()
    return None


failed = []
for path in FILES:
    sha = get_sha(path)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    args = {"owner": OWNER, "repo": REPO, "path": path, "content": content,
            "message": MSG, "branch": BRANCH}
    if sha:
        args["sha"] = sha
    else:
        print(f"{path}: no remote SHA (new file?)", flush=True)
    r = gh(["call-tool", "--name", "create_or_update_file",
            "--arguments-json", json.dumps(args)])
    ok = r.returncode == 0 and '"ok":true' in r.stdout.replace(" ", "")
    print(f"{path}: {'OK' if ok else 'FAIL'}", flush=True)
    if not ok:
        print(r.stdout[:400], flush=True)
        print(r.stderr[:400], flush=True)
        failed.append(path)

print(f"\n{len(FILES) - len(failed)}/{len(FILES)} pushed. "
      f"Failed: {failed if failed else 'none'}")
sys.exit(0 if not failed else 1)
