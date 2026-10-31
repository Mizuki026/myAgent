"""Controlled timeout and output probes; only touch the supplied directory."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["parent", "child", "output"])
parser.add_argument("directory", type=Path)
args = parser.parse_args()
args.directory.mkdir(parents=True, exist_ok=True)
if args.mode == "output":
    for _ in range(1024):
        print("x" * 1024, flush=True)
elif args.mode == "parent":
    child = subprocess.Popen(
        [sys.executable, "-B", __file__, "child", str(args.directory)],
        stdin=subprocess.DEVNULL,
    )
    (args.directory / "pids.json").write_text(
        json.dumps([os.getpid(), child.pid]), encoding="utf-8"
    )
    while True:
        time.sleep(0.05)
else:
    while True:
        with (args.directory / "heartbeat").open("ab") as stream:
            stream.write(b".")
        time.sleep(0.05)
