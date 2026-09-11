"""Diagnostics against Xiaomi Cloud using secrets/config.json.

    python tools/cloud_probe.py records [--limit 5]      # latest weighings shown in Xiaomi Home
    python tools/cloud_probe.py unclaimed [--limit 5]    # weighings waiting to be claimed
    python tools/cloud_probe.py upload weighing.json     # save one weighing like the Pico does
    python tools/cloud_probe.py call /path '{"json":1}'  # raw signed API call
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pico"))
import uploader  # noqa: E402
import xmcloud  # noqa: E402

CFG = os.path.join(ROOT, "secrets", "config.json")
SESSION = os.path.join(ROOT, "secrets", "session_pc.json")


def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return default


def make_cloud(cfg):
    x = cfg["xiaomi"]
    s = load(SESSION, {})
    c = xmcloud.MiCloud(x["region"], x["user_id"], x["pass_token"], s.get("ssecurity"), s.get("service_token"),
                        x.get("device_id"))

    def saved(cl):
        with open(SESSION, "w") as f:
            json.dump({"ssecurity": cl.ssecurity, "service_token": cl.service_token}, f)
        if cl.pass_token != cfg["xiaomi"]["pass_token"]:
            cfg["xiaomi"]["pass_token"] = cl.pass_token
            with open(CFG, "w") as f:
                json.dump(cfg, f, indent=2)
            print("!! passToken changed: config.json updated, deploy it to the Pico again")

    c.on_session = saved
    return c


def print_items(items, limit):
    for it in items[:limit]:
        d = json.loads(it.get("data") or "{}")
        print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(it.get("createTime", 0)) / 1000)),
              "weight=%s hr=%s bfp=%s" % (d.get("weight"), d.get("heartRate"), d.get("bfp")),
              "profile=%s" % d.get("duid"), "source=%s" % ("phone" if "idx" in d else "gateway/app"))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("records", "unclaimed"):
        p = sub.add_parser(name)
        p.add_argument("--limit", type=int, default=5)
    u = sub.add_parser("upload")
    u.add_argument("file")
    c = sub.add_parser("call")
    c.add_argument("path")
    c.add_argument("params")
    a = ap.parse_args()

    cfg = load(CFG)
    if not cfg:
        sys.exit("secrets/config.json not found: run tools/setup_gateway.py first")
    cloud = make_cloud(cfg)
    sc = cfg["scale"]
    hdr = {"MIOT-REQUEST-MODEL": sc.get("model") or uploader.DEFAULT_MODEL}
    now_ms = int(time.time() * 1000)

    if a.cmd == "records":
        # did=0/accountId=0 can miss the newest records: query with the real ones
        res = cloud.api("/eco/common/scale/getUserDataByPage", {
            "endTime": 1, "beginTime": now_ms, "model": hdr["MIOT-REQUEST-MODEL"], "uid": cloud.user_id,
            "did": sc["did"], "accountId": int(cloud.user_id)}, hdr)
        print_items(res.get("result") or [], a.limit)
    elif a.cmd == "unclaimed":
        res = cloud.api("/eco/common/scale/getUnClaimByPage", {
            "model": hdr["MIOT-REQUEST-MODEL"], "uid": int(cloud.user_id), "accountId": 0, "did": sc["did"],
            "beginTime": now_ms, "endTime": 1, "pageSize": 20}, hdr)
        print_items(res.get("result") or [], a.limit)
    elif a.cmd == "upload":
        ok, info = uploader.upload(cloud, cfg, load(a.file))
        print("OK" if ok else "FAIL", json.dumps(info))
    elif a.cmd == "call":
        print(json.dumps(cloud.api(a.path, json.loads(a.params), hdr), indent=1, ensure_ascii=False)[:5000])


if __name__ == "__main__":
    main()
