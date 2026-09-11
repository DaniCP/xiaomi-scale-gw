# Saves a finished weighing in Xiaomi Cloud exactly like the official Mi Home scale plugin does:
#   1. /eco/scale/account/list      -> family member whose accountCode == scale profile id
#   2. /eco/scale/bcalc             -> body composition from weight + 50/250 kHz impedances
#   3. /eco/common/scale/add        -> the record shown in the app (fromSource 2)
#   4. /eco/scale/account/updateWeight -> reference weight used by the scale to recognise the user
# Weighings from an unrecognised profile go to the "unclaimed" list (/eco/common/scale/batchAddClaim).
import json
import time

DEFAULT_MODEL = "yunmai.scales.ms104"
COMP_FLOAT = ("bfp", "slm", "bwp", "bmc", "pp", "smm", "bmi", "swt", "mc", "wc", "fc", "whr",
              "slp", "bmcp", "bfm", "ffm", "bwm", "pm")
COMP_STR = ("vfl", "bmr", "bt", "ma", "sbc")


class UploadError(Exception):
    pass


def _ok(res, what):
    if not isinstance(res, dict) or res.get("code") != 0:
        raise UploadError("%s failed: %s" % (what, json.dumps(res)[:200]))
    return res.get("result")


def age_at(birth_ms, ts):
    b = time.gmtime(int(birth_ms) // 1000 + 43200)  # birth is local midnight in ms; +12h avoids TZ day slips
    n = time.gmtime(int(ts))
    age = n[0] - b[0]
    if (n[1], n[2]) < (b[1], b[2]):
        age -= 1
    return age


def bmi(weight, height_cm):
    h = float(height_cm) / 100
    return int(weight / (h * h) * 10 + 0.5) / 10 if h > 0 else 0


def base_data(uid, m):
    return {
        "miid": str(uid), "duid": str(m.get("profile") or 0), "userType": "1",
        "weight": m["weight"], "heartRate": str(m.get("hr") or 0), "status": "0",
        "time": str(int(m.get("ts") or m["t"])),
        "bodyRes": m.get("imp_low") or 0, "bodyRes2": m.get("imp_high") or 0,
    }


def upload(cloud, cfg, m):
    """Returns (ok, info). Raises on network/auth errors so the caller retries later."""
    sc = cfg["scale"]
    model = sc.get("model") or DEFAULT_MODEL
    did = sc["did"]
    hdr = {"MIOT-REQUEST-MODEL": model}
    uid = int(cloud.user_id)
    ts = int(m.get("ts") or m["t"])
    data = base_data(uid, m)

    users = _ok(cloud.api("/eco/scale/account/list", {"uid": uid, "deviceId": did, "source": 2}, hdr), "account/list")
    profile = m.get("profile") or 0
    user = None
    for u in users or []:
        if profile and int(u.get("accountCode", -1)) == profile:
            user = u
    if user is None:
        for k in COMP_FLOAT:
            data[k] = 0
        for k in COMP_STR:
            data[k] = "0"
        rec = {"model": model, "uid": 0, "accountId": 0, "did": did, "createTime": ts * 1000,
               "data": json.dumps(data), "dataVersion": 1, "sn": "", "fromSource": 2}
        _ok(cloud.api("/eco/common/scale/batchAddClaim", [rec], hdr), "batchAddClaim")
        return True, {"unclaimed": True, "profile": profile}

    account_id = int(user.get("accountId") or 0) or uid
    data["userType"] = str(user.get("type", 1))
    comp = None
    age = age_at(user["birth"], ts)
    if data["bodyRes"] > 0 and data["bodyRes2"] > 0 and 6 <= age <= 80:
        try:
            comp = _ok(cloud.api("/eco/scale/bcalc", {
                "version": "235", "age": age, "sex": 0 if int(user["sex"]) == 2 else 1,
                "height": int(float(user["height"]) * 10), "weight": int(m["weight"] * 10 + 0.5),
                "electrodes_number": 4, "res_mapping_switch": 0,
                "current_impedance": [int(data["bodyRes"] * 10 + 0.5), int(data["bodyRes2"] * 10 + 0.5)],
            }, hdr), "bcalc")
        except UploadError:
            comp = None  # the app also saves the plain weight when the calculation fails
    for k in COMP_FLOAT:
        data[k] = comp.get(k, 0) if comp else 0
    for k in COMP_STR:
        data[k] = str(comp.get(k, 0)) if comp else "0"
    if not data["bmi"]:
        data["bmi"] = bmi(m["weight"], user.get("height") or 0)
    data["user"] = user

    rec = {"model": model, "uid": uid, "accountId": account_id, "did": did, "createTime": ts * 1000,
           "data": json.dumps(data), "dataVersion": 1, "sn": sc.get("sn", ""), "fromSource": 2}
    _ok(cloud.api("/eco/common/scale/add", rec, hdr), "add")

    if ts * 1000 > int(user.get("weightUpdateTime") or 0):
        try:
            cloud.api("/eco/scale/account/updateWeight", {
                "uid": uid, "weightTarget": m["weight"], "accountId": account_id, "weightUpdateTime": ts * 1000}, hdr)
        except Exception:
            pass  # the record is already saved; the reference weight is best-effort
    return True, {"accountId": account_id, "createTime": ts * 1000, "bfp": data["bfp"], "composition": comp is not None}
