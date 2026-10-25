# Minimal Garmin Connect client: uploads a weighing as a FIT weight_scale file. Runs on MicroPython and CPython.
# Auth: the PC logs in once (tools/setup_garmin.py) and hands over Garmin's mobile "DI OAuth2" tokens. The access
# token (JWT, ~25 h) is renewed with the refresh token (30 days, rotated on every renewal, so it must be saved).
import binascii
import hashlib
import json
import os
import struct
import time

from xmcloud import http_request, quote

DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
UPLOAD_URL = "https://connectapi.garmin.com/upload-service/upload"
# headers of the Garmin Connect Android app, as sent by python-garminconnect
NATIVE_HEADERS = {
    "User-Agent": "GCM-Android-5.23",
    "X-Garmin-User-Agent": "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; "
                           "Android/33; Dalvik/2.1.0",
    "X-Garmin-Paired-App-Version": "10861",
    "X-Garmin-Client-Platform": "Android",
    "X-App-Ver": "10861",
    "X-Lang": "en",
    "X-GCExperience": "GC5",
    "Accept-Language": "en-US,en;q=0.9",
}
FIT_EPOCH = 631065600  # 1989-12-31 00:00 UTC
# Xiaomi composition key -> (FIT weight_scale field, scale); None skips the field
FIELDS = (("weight", 0, 100), ("bfp", 1, 100), ("bwp", 2, 100), ("bmc", 4, 100), ("smm", 5, 100),
          ("bmr", 7, 4), ("ma", 10, 1), ("vfl", 11, 1), ("bmi", 13, 10))


class GarminError(Exception):
    pass


class AuthError(GarminError):
    """The refresh token was rejected: run tools/setup_garmin.py again."""


def b64url_decode(s):
    s = s.replace("-", "+").replace("_", "/")
    return binascii.a2b_base64(s + "=" * (-len(s) % 4))


def jwt_claims(token):
    try:
        return json.loads(b64url_decode(token.split(".")[1]).decode())
    except Exception:
        return {}


def seed_id(refresh_token):
    """Short fingerprint of the refresh token issued by the PC, to notice a new login without storing it twice."""
    return binascii.hexlify(hashlib.sha256(refresh_token.encode()).digest()[:8]).decode()


# ---- FIT encoding (same layout as python-garminconnect's FitEncoderWeight, which Garmin accepts) ----
# base types: (size, FIT code, invalid value)
_ENUM = (1, 0x00, 0xFF)
_U8 = (1, 0x02, 0xFF)
_U16 = (2, 0x84, 0xFFFF)
_U32 = (4, 0x86, 0xFFFFFFFF)
_U32Z = (4, 0x8C, 0)
_FMT = {1: "<B", 2: "<H", 4: "<I"}


def fit_crc(data, crc=0):
    t = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
         0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)
    for b in data:
        tmp = t[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ t[b & 0xF]
        tmp = t[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ t[(b >> 4) & 0xF]
    return crc


def _message(local, global_num, fields):
    """Definition + data record. fields: [(field number, base type, int value or None)]."""
    d = bytearray(struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields)))
    v = bytearray((local,))
    for num, bt, val in fields:
        d += bytes((num, bt[0], bt[1]))
        v += struct.pack(_FMT[bt[0]], bt[2] if val is None else val)
    return d + v


def _scaled(v, scale):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return int(v * scale + 0.5) if v > 0 else None  # Xiaomi uses 0 for "not measured"


def fit_weight(w):
    """FIT file (bytes) for one weighing dict: ts (unix), weight (kg) and the optional keys in FIELDS."""
    ts = int(w["ts"]) - FIT_EPOCH
    body = _message(0, 0, [(3, _U32Z, None), (4, _U32, ts), (1, _U16, None), (2, _U16, None),
                           (5, _U16, None), (0, _ENUM, 9)])  # file_id, type 9 = weight
    body += _message(1, 49, [(0, _U16, None), (1, _U8, None)])  # file_creator
    body += _message(2, 23, [(253, _U32, ts), (3, _U32Z, None), (7, _U32, None), (8, _U32, None),
                             (2, _U16, None), (4, _U16, None), (5, _U16, None), (10, _U16, None),
                             (0, _U8, None), (1, _U8, None), (6, _U8, None), (11, _U8, None)])  # device_info
    vals = {}
    for key, num, scale in FIELDS:
        vals[num] = _scaled(w.get(key), scale)
    body += _message(3, 30, [(253, _U32, ts), (0, _U16, vals[0]), (1, _U16, vals[1]), (2, _U16, vals[2]),
                             (3, _U16, None), (4, _U16, vals[4]), (5, _U16, vals[5]), (7, _U16, vals[7]),
                             (9, _U16, None), (8, _U8, None), (10, _U8, vals[10]), (11, _U8, vals[11]),
                             (13, _U16, vals[13])])  # weight_scale
    out = struct.pack("<BBHI", 12, 16, 108, len(body)) + b".FIT" + body
    return out + struct.pack("<H", fit_crc(out))


def from_xiaomi(m, data):
    """Garmin queue item from a weighing and the record data saved in Xiaomi Cloud (see uploader.upload)."""
    w = {"ts": int(m.get("ts") or m["t"]), "weight": m["weight"]}
    for key, _, _ in FIELDS[1:]:
        if data.get(key):
            w[key] = data[key]
    return w


class Garmin:
    def __init__(self, access_token, refresh_token, client_id):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.on_tokens = None  # callback(self) after a renewal: persist the rotated tokens

    def expires_in(self, now=None):
        """Seconds of validity left on the access token (0 if unknown)."""
        exp = jwt_claims(self.access_token or "").get("exp")
        try:
            return int(exp) - int(now if now is not None else time.time())
        except (TypeError, ValueError):
            return 0

    def refresh(self):
        auth = binascii.b2a_base64((self.client_id + ":").encode()).rstrip(b"\n").decode()
        h = dict(NATIVE_HEADERS)
        h.update({"Authorization": "Basic " + auth, "Accept": "application/json",
                  "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
        body = "grant_type=refresh_token&client_id=%s&refresh_token=%s" % (
            quote(self.client_id), quote(self.refresh_token))
        st, _, b = http_request("POST", DI_TOKEN_URL, h, body)
        if st in (400, 401):
            raise AuthError("refresh token rejected (http %d)" % st)
        if st != 200:
            raise GarminError("token refresh failed: http %d" % st)
        j = json.loads(b.decode())
        self.access_token = j["access_token"]
        self.refresh_token = j.get("refresh_token") or self.refresh_token
        self.client_id = jwt_claims(self.access_token).get("client_id") or self.client_id
        if self.on_tokens:
            self.on_tokens(self)

    def _post_file(self, name, data):
        boundary = "scalegw" + binascii.hexlify(os.urandom(8)).decode()
        h = dict(NATIVE_HEADERS)
        h.update({"Authorization": "Bearer " + self.access_token, "Accept": "application/json",
                  "Content-Type": "multipart/form-data; boundary=" + boundary})
        body = ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode()
        body += data + ("\r\n--%s--\r\n" % boundary).encode()
        st, _, b = http_request("POST", UPLOAD_URL, h, body)
        return st, b

    def upload_weight(self, w):
        """Uploads one weighing. Returns a short info dict; raises GarminError so the caller retries later."""
        if self.expires_in() < 900:
            self.refresh()
        fit = fit_weight(w)
        st, b = self._post_file("weight.fit", fit)
        if st == 401:
            self.refresh()
            st, b = self._post_file("weight.fit", fit)
        if st == 409:
            return {"duplicate": True}
        if st not in (200, 201, 202):
            raise GarminError("upload failed: http %d %s" % (st, b[:120]))
        try:
            r = (json.loads(b.decode()) or {}).get("detailedImportResult") or {}
        except ValueError:
            r = {}
        info = {"http": st, "id": r.get("uploadId")}
        if r.get("failures"):
            raise GarminError("upload rejected: %s" % json.dumps(r["failures"])[:160])
        return info
