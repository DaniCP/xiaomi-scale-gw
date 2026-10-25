"""Optional: also send the weighings to Garmin Connect.

Logs into Garmin Connect once on the PC (python-garminconnect handles Garmin's login and MFA), checks that the
gateway's own code can renew the session, and adds the tokens to secrets/config.json. Run tools/setup_gateway.py
first. Your Garmin password is only sent to Garmin and is not stored.

    python tools/setup_garmin.py [--profile N] [--deploy] [--port COM5]
"""
import argparse
import getpass
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pico"))
import garmin  # noqa: E402


def garmin_login():
    try:
        from garminconnect import Garmin
    except ImportError:
        sys.exit("Missing dependency: python -m pip install -r requirements.txt")
    os.environ.pop("GARMINTOKENS", None)  # never read or write a token store on the PC
    print("\n1) Garmin Connect login")
    email = input("   Email: ").strip()
    pw = getpass.getpass("   Password (hidden): ")
    api = Garmin(email, pw, prompt_mfa=lambda: input("   MFA code (sent by Garmin): ").strip())
    api.login()
    c = api.client
    if not (c.di_token and c.di_refresh_token and c.di_client_id):
        sys.exit("Garmin accepted the login but returned no mobile (DI) tokens; the gateway can't use this session.")
    try:
        name = api.get_full_name() or api.display_name
    except Exception:
        name = "?"
    print("   Logged in as %s" % name)
    return c.di_token, c.di_refresh_token, c.di_client_id


def ask_profile(default):
    print("\n2) Which person's weighings go to this Garmin account?")
    print("   The scale numbers each person it recognises (the Pico log shows 'scale: profile N'); 0 = everyone.")
    while True:
        n = input("   Profile [%d]: " % default).strip() or str(default)
        if n.isdigit():
            return int(n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(ROOT, "secrets", "config.json"))
    ap.add_argument("--profile", type=int, help="scale profile to forward (0 = every recognised person)")
    ap.add_argument("--remove", action="store_true", help="remove the Garmin login from the configuration")
    ap.add_argument("--deploy", action="store_true", help="copy firmware + config to the Pico when done")
    ap.add_argument("--port", default="auto", help="Pico serial port (default: auto-detect)")
    a = ap.parse_args()
    try:
        with open(a.config) as f:
            cfg = json.load(f)
    except OSError:
        sys.exit("%s not found: run tools/setup_gateway.py first." % a.config)

    if a.remove:
        cfg.pop("garmin", None)
        print("Garmin removed from %s." % a.config)
    else:
        access, refresh, client_id = garmin_login()
        profile = a.profile if a.profile is not None else ask_profile((cfg.get("garmin") or {}).get("profile", 1))

        print("\n3) Checking token renewal with the gateway code (what the Pico will do)...")
        g = garmin.Garmin(access, refresh, client_id)
        g.refresh()
        print("   OK (session valid for %.1f h, renewed automatically by the Pico)" % (g.expires_in(time.time()) / 3600))
        cfg["garmin"] = {"profile": profile, "access_token": g.access_token, "refresh_token": g.refresh_token,
                         "client_id": g.client_id}

    with open(a.config, "w") as f:
        json.dump(cfg, f, indent=2)
    print("\nConfiguration saved to %s (it contains credentials: never share it)." % a.config)

    if a.deploy:
        print("\nCopying firmware and configuration to the Pico...")
        sys.exit(subprocess.call([sys.executable, os.path.join(ROOT, "tools", "deploy.py"),
                                  "--port", a.port, "--config", a.config]))
    print("Next: python tools/deploy.py --config %s" % a.config)


if __name__ == "__main__":
    main()
