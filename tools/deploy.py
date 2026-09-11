"""Copy the gateway firmware (pico/*.py) and, optionally, config.json to the Pico W, then reset it.

    python tools/deploy.py [--port auto|COM5|/dev/ttyACM0] [--config secrets/config.json] [--no-reset]
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# main.py goes last so a half-finished copy never boots
FILES = ["mibeacon.py", "xmcloud.py", "scale.py", "uploader.py", "main.py"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="auto", help="serial port of the Pico (default: auto-detect)")
    ap.add_argument("--config", help="config.json to upload (contains secrets)")
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()

    cmd = [sys.executable, "-m", "mpremote", "connect", a.port]
    steps = []
    if a.config:
        steps.append(["fs", "cp", a.config, ":config.json"])
    for f in FILES:
        steps.append(["fs", "cp", os.path.join(ROOT, "pico", f), ":" + f])
    if not a.no_reset:
        steps.append(["reset"])
    for i, s in enumerate(steps):
        cmd += (["+"] if i else []) + s
    print("> mpremote connect %s: %d steps%s" % (a.port, len(steps), " (with config)" if a.config else ""))
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
