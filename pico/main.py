# Xiaomi Body Composition Scale S400 -> Xiaomi Cloud (and optionally Garmin Connect) gateway for Raspberry Pi Pico W.
import binascii
import gc
import json
import os
import time

import bluetooth
import machine
import network
import ntptime

import garmin
import mibeacon
import scale
import uploader
import xmcloud

CFG_FILE = "config.json"
SESSION_FILE = "session.json"
QUEUE_FILE = "queue.json"
GARMIN_FILE = "garmin.json"
GQUEUE_FILE = "garmin_queue.json"
NTP_EVERY_S = 6 * 3600
RETRY_S = (10, 30, 60, 300, 900)
GARMIN_RENEW_S = 6 * 3600  # renew the Garmin token when less is left: rotates the 30-day refresh token daily
GARMIN_CHECK_S = 3600
GARMIN_AUTH_RETRY_S = 6 * 3600

led = machine.Pin("LED", machine.Pin.OUT)


def log(*a):
    t = time.gmtime()
    print("%02d:%02d:%02d" % (t[3], t[4], t[5]), *a)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.rename(tmp, path)


def blink(n, on_ms=80, off_ms=120):
    for _ in range(n):
        led.on()
        time.sleep_ms(on_ms)
        led.off()
        time.sleep_ms(off_ms)


cfg = load_json(CFG_FILE, None)
if not cfg:
    log("config.json missing: run tools/setup_gateway.py on the PC")
    while True:
        blink(3)
        time.sleep(2)

wlan = network.WLAN(network.STA_IF)


def wifi_up(timeout_s=30):
    if wlan.isconnected():
        return True
    wlan.active(True)
    try:
        network.hostname("scale-gw")
    except Exception:
        pass
    wlan.config(pm=0xA11140)  # disable power saving: keeps BLE+WiFi coexistence stable
    log("wifi: connecting to", cfg["wifi"]["ssid"])
    wlan.connect(cfg["wifi"]["ssid"], cfg["wifi"]["password"])
    t0 = time.ticks_ms()
    while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), t0) < timeout_s * 1000:
        led.toggle()
        time.sleep_ms(250)
    led.off()
    if wlan.isconnected():
        log("wifi: ip", wlan.ifconfig()[0])
        return True
    log("wifi: failed, status", wlan.status())
    return False


def sync_time():
    for _ in range(5):
        try:
            ntptime.settime()
            log("ntp: ok")
            return True
        except Exception as e:
            log("ntp:", e)
            time.sleep(2)
    return False


xc = cfg["xiaomi"]
sess = load_json(SESSION_FILE, {})
cloud = xmcloud.MiCloud(
    xc["region"], xc["user_id"], sess.get("pass_token") or xc["pass_token"],
    sess.get("ssecurity"), sess.get("service_token"), xc.get("device_id"))


def _save_session(c):
    save_json(SESSION_FILE, {"pass_token": c.pass_token, "ssecurity": c.ssecurity, "service_token": c.service_token})
    log("cloud: session refreshed")


cloud.on_session = _save_session

gcfg = cfg.get("garmin") or {}
gm = None
if gcfg.get("refresh_token"):
    _gseed = garmin.seed_id(gcfg["refresh_token"])
    gs = load_json(GARMIN_FILE, {})
    if gs.get("seed") != _gseed:
        gs = {}  # new login on the PC: start again from the tokens in config.json
    gm = garmin.Garmin(gs.get("access_token") or gcfg["access_token"], gs.get("refresh_token") or gcfg["refresh_token"],
                       gs.get("client_id") or gcfg["client_id"])

    def _save_garmin(g):
        save_json(GARMIN_FILE, {"seed": _gseed, "access_token": g.access_token, "refresh_token": g.refresh_token,
                                "client_id": g.client_id})
        log("garmin: tokens refreshed")

    gm.on_tokens = _save_garmin

MAC = binascii.unhexlify(cfg["scale"]["mac"].replace(":", ""))
tracker = scale.ScaleTracker(MAC, binascii.unhexlify(cfg["scale"]["bindkey"]))
frames = []
ble = bluetooth.BLE()
scanning = False


def bt_irq(event, data):
    global scanning
    if event == 5:  # _IRQ_SCAN_RESULT
        if bytes(data[1]) != MAC:
            return
        sd = mibeacon.find_fe95(data[4])
        if sd and len(frames) < 64:
            frames.append(sd)
    elif event == 6:  # _IRQ_SCAN_DONE
        scanning = False


def scan(on):
    global scanning
    if on and not scanning:
        ble.gap_scan(0, 60000, 40000, False)  # passive, forever
        scanning = True
    elif not on and scanning:
        ble.gap_scan(None)
        scanning = False


queue = load_json(QUEUE_FILE, [])
gqueue = load_json(GQUEUE_FILE, [])
retry_idx = 0
next_try = 0
g_retry_idx = 0
g_next_try = 0


def to_garmin(m, res, data):
    global g_next_try
    if not gm or not data or res.get("unclaimed") or gcfg.get("profile", 0) not in (0, m.get("profile")):
        return
    gqueue.append(garmin.from_xiaomi(m, data))
    save_json(GQUEUE_FILE, gqueue)
    g_next_try = 0


def flush_queue():
    global retry_idx, next_try
    if not wifi_up():
        return False
    scan(False)
    gc.collect()
    ok_all = True
    try:
        while queue:
            m = queue[0]
            ok, res = uploader.upload(cloud, cfg, m)
            data = res.pop("data", None)
            log("cloud: upload %.1f kg ->" % m["weight"], "OK" if ok else "FAIL", json.dumps(res)[:160])
            if not ok:
                ok_all = False
                break
            to_garmin(m, res, data)
            data = None
            queue.pop(0)
            save_json(QUEUE_FILE, queue)
            blink(2, 300, 150)
    except Exception as e:
        ok_all = False
        log("cloud: error", repr(e))
    finally:
        gc.collect()
        scan(True)
    if ok_all:
        retry_idx = 0
    else:
        next_try = time.time() + RETRY_S[min(retry_idx, len(RETRY_S) - 1)]
        retry_idx += 1
    return ok_all


def garmin_sync(now):
    """Uploads pending Garmin weighings and renews the token before it expires."""
    global g_retry_idx, g_next_try
    if not wifi_up():
        g_next_try = time.time() + RETRY_S[-1]
        return
    scan(False)
    gc.collect()
    wait = GARMIN_CHECK_S
    try:
        if gm.expires_in(now) < GARMIN_RENEW_S:
            gm.refresh()
        while gqueue:
            w = gqueue[0]
            res = gm.upload_weight(w)
            log("garmin: upload %.1f kg -> OK" % w["weight"], json.dumps(res))
            gqueue.pop(0)
            save_json(GQUEUE_FILE, gqueue)
            blink(3, 100, 100)
        g_retry_idx = 0
    except garmin.AuthError as e:
        log("garmin: login expired, run tools/setup_garmin.py again:", e)
        wait = GARMIN_AUTH_RETRY_S
    except Exception as e:
        log("garmin: error", repr(e))
        wait = RETRY_S[min(g_retry_idx, len(RETRY_S) - 1)]
        g_retry_idx += 1
    finally:
        gc.collect()
        scan(True)
    g_next_try = time.time() + wait


def main():
    global next_try, g_next_try
    blink(1, 500)
    while not wifi_up():
        time.sleep(10)
    sync_time()
    last_ntp = time.time()
    ble.active(True)
    ble.irq(bt_irq)
    scan(True)
    log("gateway ready, listening for", cfg["scale"]["mac"], "queue:", len(queue),
        "garmin:", ("queue %d" % len(gqueue)) if gm else "off")
    last_sd = None
    while True:
        while frames:
            sd = frames.pop(0)
            if sd == last_sd:
                continue
            last_sd = sd
            for d in tracker.feed(sd, time.time()):
                led.toggle()
                log("scale: profile %d weight %.1f kg hr %d imp %.1f raw %s" % (
                    d["profile"], d["weight"], d["hr"], d["imp"], d["hex"]))
            if tracker.last_error and tracker.last_error != "no key":
                log("scale: frame error", tracker.last_error)
        now = time.time()
        tracker.poll(now)
        for m in tracker.pop_done():
            led.off()
            md = m.to_dict()
            log("scale: weighing done %.1f kg hr %d imp %.1f/%.1f complete=%s objs=%d" % (
                m.weight, m.hr, m.imp_low, m.imp_high, m.complete, len(m.records)))
            queue.append(md)
            save_json(QUEUE_FILE, queue)
            next_try = 0
        if queue and now >= next_try and tracker.cur is None:
            flush_queue()
        if gm and now >= g_next_try and tracker.cur is None:
            if gqueue or gm.expires_in(now) < GARMIN_RENEW_S:
                garmin_sync(now)
            else:
                g_next_try = now + GARMIN_CHECK_S
        if now - last_ntp > NTP_EVERY_S:
            if wifi_up() and sync_time():
                last_ntp = now
        if not scanning:
            scan(True)
        time.sleep_ms(50)


try:
    main()
except KeyboardInterrupt:
    scan(False)
    raise
except Exception as e:
    log("fatal:", repr(e))
    time.sleep(10)
    machine.reset()
