"""Setup assistant for the S400 -> Xiaomi Cloud gateway.

Logs into your Xiaomi account by scanning a QR code with the Xiaomi Home app (your password is never typed
here), finds the scale, fetches its bindkey and writes secrets/config.json for the Pico W.

    python tools/setup_gateway.py [--deploy] [--port COM5] [--wifi-ssid NAME] [--mac AA:BB:CC:DD:EE:FF]
"""
import argparse
import binascii
import getpass
import json
import os
import pathlib
import subprocess
import sys
import time
import webbrowser

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pico"))
import xmcloud  # noqa: E402

REGIONS = ["de", "i2", "cn", "us", "ru", "sg", "in", "tw"]
S400_MODELS = ("yunmai.scales.ms103", "yunmai.scales.ms104")


def _json(text):
    return json.loads(text.replace("&&&START&&&", "", 1))


def qr_login(out_dir):
    s = requests.Session()
    r = s.get("https://account.xiaomi.com/longPolling/loginUrl", params={
        "_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue", "callback": "https://sts.api.io.mi.com/sts",
        "_hasLogo": "false", "sid": "xiaomiio", "serviceParam": "", "_locale": "en_US",
        "_dc": str(int(time.time() * 1000))}, timeout=20)
    j = _json(r.text)
    qr_path = os.path.join(out_dir, "xiaomi_login_qr.png")
    with open(qr_path, "wb") as f:
        f.write(s.get(j["qr"], timeout=20).content)
    print("\n1) Open the Xiaomi Home app on your phone and scan this QR code (opening it now):")
    print("   ", qr_path)
    print("   Or open this URL on the phone:", j["loginUrl"])
    try:
        webbrowser.open(pathlib.Path(qr_path).resolve().as_uri())
    except Exception:
        pass
    deadline = time.time() + int(j.get("timeout", 300))
    d = None
    while time.time() < deadline:
        try:
            r = s.get(j["lp"], timeout=15)
        except requests.exceptions.Timeout:
            continue
        if r.status_code == 200:
            d = _json(r.text)
            if d.get("passToken"):
                break
        time.sleep(1)
    try:
        os.remove(qr_path)
    except OSError:
        pass
    if not d or not d.get("passToken"):
        sys.exit("The QR code expired before the login finished. Run the script again.")
    r = s.get(d["location"], timeout=20)
    token = r.cookies.get("serviceToken") or s.cookies.get("serviceToken")
    print("   Logged in (userId %s)" % d["userId"])
    return str(d["userId"]), d["passToken"], d["ssecurity"], token


def find_scales(cloud, mac):
    """Returns (region, [devices]) for the first region that has matching scales."""
    for region in REGIONS:
        cloud.region = region
        try:
            res = cloud.api("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 0})
        except Exception as e:
            print("   server %-3s error: %s" % (region, e))
            continue
        devs = (res.get("result") or {}).get("list") or []
        print("   server %-3s %d devices" % (region, len(devs)))
        if mac:
            found = [d for d in devs if (d.get("mac") or "").upper() == mac.upper()]
        else:
            found = [d for d in devs if d.get("model") in S400_MODELS]
        if found:
            return region, found
    return None, []


def pick(devs):
    if len(devs) == 1:
        return devs[0]
    for i, d in enumerate(devs, 1):
        print("   [%d] %s  %s  %s" % (i, d.get("name"), d.get("model"), d.get("mac")))
    while True:
        n = input("   Which scale? [1-%d]: " % len(devs)).strip()
        if n.isdigit() and 1 <= int(n) <= len(devs):
            return devs[int(n) - 1]


def latest_sn(cloud, dev):
    hdr = {"MIOT-REQUEST-MODEL": dev["model"]}
    try:
        res = cloud.api("/eco/common/scale/getUserDataByPage", {
            "endTime": 1, "beginTime": int(time.time() * 1000), "model": dev["model"], "uid": cloud.user_id,
            "did": dev["did"], "accountId": int(cloud.user_id)}, hdr)
        for it in res.get("result") or []:
            if it.get("sn"):
                return it["sn"]
    except Exception:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mac", help="scale MAC, only needed if the account has several S400 scales")
    ap.add_argument("--wifi-ssid", help="2.4 GHz Wi-Fi network name (asked if omitted)")
    ap.add_argument("--out", default=os.path.join(ROOT, "secrets", "config.json"))
    ap.add_argument("--deploy", action="store_true", help="copy firmware + config to the Pico when done")
    ap.add_argument("--port", default="auto", help="Pico serial port (default: auto-detect)")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    user_id, pass_token, ssecurity, service_token = qr_login(os.path.dirname(a.out))
    device_id = binascii.hexlify(os.urandom(8)).decode()
    cloud = xmcloud.MiCloud("de", user_id, pass_token, ssecurity, service_token, device_id)

    print("\n2) Looking for your S400 scale...")
    region, devs = find_scales(cloud, a.mac)
    if not devs:
        sys.exit("No S400 scale found in this account. Is it added to Xiaomi Home with this account?")
    dev = pick(devs)
    print("   Found: %s | %s | %s | server %s" % (dev.get("name"), dev.get("model"), dev.get("mac"), region))

    res = cloud.api("/v2/device/blt_get_beaconkey", {"did": dev["did"], "pdid": 1})
    bindkey = ((res.get("result") or {}).get("beaconkey") or "").lower()
    if len(bindkey) != 32:
        sys.exit("Could not get the scale bindkey (response: %s)" % res)
    print("   Bindkey OK")

    hdr = {"MIOT-REQUEST-MODEL": dev["model"]}
    users = cloud.api("/eco/scale/account/list", {"uid": int(user_id), "deviceId": dev["did"], "source": 2}, hdr)
    if not users.get("result"):
        print("   WARNING: no user profile in the scale plugin yet. Weigh yourself once with the app,"
              " otherwise weighings will be saved as unclaimed.")
    sn = latest_sn(cloud, dev)

    print("\n3) Checking session renewal with the passToken (what the Pico will do)...")
    cloud.login()
    print("   OK")

    print("\n4) Wi-Fi (2.4 GHz)")
    ssid = a.wifi_ssid or input("   Network name (SSID): ").strip()
    while True:
        pw = getpass.getpass("   Password (hidden): ")
        if pw and pw == getpass.getpass("   Repeat password: "):
            break
        print("   Passwords don't match, try again.")

    cfg = {
        "wifi": {"ssid": ssid, "password": pw},
        "xiaomi": {"region": region, "user_id": cloud.user_id, "pass_token": cloud.pass_token, "device_id": device_id},
        "scale": {"mac": (dev.get("mac") or "").upper(), "did": dev["did"], "model": dev.get("model"), "sn": sn,
                  "bindkey": bindkey},
    }
    with open(a.out, "w") as f:
        json.dump(cfg, f, indent=2)
    print("\nConfiguration saved to %s (it contains credentials: never share it)." % a.out)

    if a.deploy:
        print("\n5) Copying firmware and configuration to the Pico...")
        sys.exit(subprocess.call([sys.executable, os.path.join(ROOT, "tools", "deploy.py"),
                                  "--port", a.port, "--config", a.out]))
    print("Next: python tools/deploy.py --config %s" % a.out)


if __name__ == "__main__":
    main()
