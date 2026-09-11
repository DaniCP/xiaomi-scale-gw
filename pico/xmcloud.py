# Minimal Xiaomi Cloud (Mi Home) client. Runs on MicroPython (Pico W) and CPython.
# Auth: long-lived passToken -> ssecurity + serviceToken; API calls use the RC4 "encrypted" protocol.
import binascii
import hashlib
import json
import os
import socket
import ssl
import sys
import time

MICROPY = sys.implementation.name == "micropython"
UA = "Android-7.1.1-1.0.0-ONEPLUS A3010-136-PICOGW APP/xiaomi.smarthome APPV/62830"
_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"


class MiCloudError(Exception):
    pass


class AuthError(MiCloudError):
    pass


def b64e(b):
    return binascii.b2a_base64(b).rstrip(b"\n").decode()


def b64d(s):
    return binascii.a2b_base64(s)


def quote(s):
    if isinstance(s, str):
        s = s.encode()
    return "".join(chr(c) if chr(c) in _SAFE else "%%%02X" % c for c in s)


def rc4(key, data):
    """RC4 with the first 1024 keystream bytes dropped (Xiaomi flavour)."""
    S = bytearray(range(256))
    j = 0
    kl = len(key)
    for i in range(256):
        j = (j + S[i] + key[i % kl]) & 0xFF
        S[i], S[j] = S[j], S[i]
    i = j = 0
    for _ in range(1024):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(len(data))
    for k in range(len(data)):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[k] = data[k] ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


def _sendall(s, data):
    if not MICROPY:
        s.sendall(data)
        return
    mv = memoryview(data)
    off = 0
    while off < len(data):
        n = s.write(mv[off:])
        off += n or 0


def _dechunk(body):
    out = bytearray()
    pos = 0
    while True:
        eol = body.find(b"\r\n", pos)
        if eol < 0:
            break
        size = int(body[pos:eol].split(b";")[0], 16)
        if size == 0:
            break
        out += body[eol + 2:eol + 2 + size]
        pos = eol + 2 + size + 2
    return bytes(out)


def http_request(method, url, headers=None, body=None, timeout=30):
    """Tiny HTTP/1.0 client -> (status, [(header_lower, value)], body_bytes). Never follows redirects."""
    scheme, _, host, path = url.split("/", 3)
    path = "/" + path
    port = 443 if scheme == "https:" else 80
    if ":" in host:
        host, port = host.split(":")
        port = int(port)
    addr = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    chunks = []
    try:
        s.connect(addr)
        if scheme == "https:":
            if MICROPY:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.verify_mode = ssl.CERT_NONE
            else:
                ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=host)
        if isinstance(body, str):
            body = body.encode()
        req = "%s %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n" % (method, path, host)
        for k in headers or {}:
            req += "%s: %s\r\n" % (k, headers[k])
        if body is not None:
            req += "Content-Length: %d\r\n" % len(body)
        _sendall(s, (req + "\r\n").encode())
        if body:
            _sendall(s, body)
        while True:
            d = s.read(1024) if MICROPY else s.recv(4096)
            if not d:
                break
            chunks.append(d)
    finally:
        s.close()
    raw = b"".join(chunks)
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        raise MiCloudError("bad http response")
    lines = raw[:sep].decode().split("\r\n")
    status = int(lines[0].split(" ")[1])
    hs = []
    for ln in lines[1:]:
        c = ln.find(":")
        if c > 0:
            hs.append((ln[:c].strip().lower(), ln[c + 1:].strip()))
    body = raw[sep + 4:]
    for k, v in hs:
        if k == "transfer-encoding" and "chunked" in v.lower():
            body = _dechunk(body)
    return status, hs, body


def _header(hs, name):
    for k, v in hs:
        if k == name:
            return v
    return None


def api_base(region):
    return "https://api.io.mi.com/app" if region in ("", "cn") else "https://%s.api.io.mi.com/app" % region


def _login_json(body):
    if body.startswith(b"&&&START&&&"):
        body = body[11:]
    return json.loads(body.decode())


class MiCloud:
    def __init__(self, region, user_id, pass_token, ssecurity=None, service_token=None, device_id=None):
        self.region = region
        self.user_id = str(user_id)
        self.pass_token = pass_token
        self.ssecurity = ssecurity
        self.service_token = service_token
        self.device_id = device_id or binascii.hexlify(os.urandom(8)).decode()
        self.on_session = None  # callback(self) when tokens change

    # ---- auth -------------------------------------------------------------
    def login(self):
        """Refresh ssecurity/serviceToken using the stored passToken (no password, no 2FA)."""
        cookie = "userId=%s; passToken=%s; deviceId=%s; sdkVersion=accountsdk-18.8.15" % (
            self.user_id, self.pass_token, self.device_id)
        st, hs, body = http_request(
            "GET", "https://account.xiaomi.com/pass/serviceLogin?_json=true&sid=xiaomiio",
            {"Cookie": cookie, "User-Agent": UA})
        j = _login_json(body)
        if not j.get("ssecurity") or not j.get("location"):
            raise AuthError("passToken rejected (code=%s, desc=%s)" % (j.get("code"), j.get("desc")))
        return self._finish_login(j)

    def _finish_login(self, j):
        self.ssecurity = j["ssecurity"]
        if j.get("passToken"):
            self.pass_token = j["passToken"]
        if j.get("userId"):
            self.user_id = str(j["userId"])
        loc = j["location"]
        token = None
        for _ in range(4):
            st, hs, body = http_request("GET", loc, {"User-Agent": UA})
            for k, v in hs:
                if k == "set-cookie" and v.startswith("serviceToken="):
                    token = v[13:].split(";")[0]
            nxt = _header(hs, "location")
            if token or st not in (301, 302, 303, 307) or not nxt:
                break
            loc = nxt
        if not token:
            raise AuthError("no serviceToken from STS (http %s)" % st)
        self.service_token = token
        if self.on_session:
            self.on_session(self)
        return True

    # ---- signed API -------------------------------------------------------
    def api(self, path, params, headers=None, _retry=True):
        """POST an RC4-encrypted request to /app<path>. Returns the decoded JSON dict."""
        if not self.service_token or not self.ssecurity:
            self.login()
        data = json.dumps(params)
        nonce = os.urandom(8) + int(time.time() // 60).to_bytes(4, "big")
        snonce = hashlib.sha256(b64d(self.ssecurity) + nonce).digest()
        sn64 = b64e(snonce)
        h = b64e(hashlib.sha1(("POST&%s&data=%s&%s" % (path, data, sn64)).encode()).digest())
        enc_data = b64e(rc4(snonce, data.encode()))
        enc_h = b64e(rc4(snonce, h.encode()))
        sig = b64e(hashlib.sha1(
            ("POST&%s&data=%s&rc4_hash__=%s&%s" % (path, enc_data, enc_h, sn64)).encode()).digest())
        body = "data=%s&rc4_hash__=%s&signature=%s&_nonce=%s" % (
            quote(enc_data), quote(enc_h), quote(sig), quote(b64e(nonce)))
        hdr = {
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "identity",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
            "Cookie": "userId=%s; yetAnotherServiceToken=%s; serviceToken=%s; locale=en_US; timezone=GMT+01:00; channel=MI_APP_STORE" % (
                self.user_id, self.service_token, self.service_token),
        }
        if headers:
            hdr.update(headers)
        st, hs, rbody = http_request("POST", api_base(self.region) + path, hdr, body)
        if st in (401, 403) and _retry:
            self.service_token = None
            self.login()
            return self.api(path, params, headers, False)
        if st != 200:
            raise MiCloudError("http %s: %s" % (st, rbody[:200]))
        try:
            plain = rc4(snonce, b64d(rbody))
            return json.loads(plain.decode())
        except Exception:
            return json.loads(rbody.decode())

    # ---- helpers ----------------------------------------------------------
    def set_user_device_data(self, records):
        return self.api("/user/set_user_device_data", records)

    def get_user_device_data(self, did, key, typ="event", seconds=86400 * 7, limit=20):
        now = int(time.time())
        return self.api("/user/get_user_device_data", {
            "did": did, "key": key, "type": typ,
            "time_start": now - seconds, "time_end": now + 60, "limit": limit})
