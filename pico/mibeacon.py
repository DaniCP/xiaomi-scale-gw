# MiBeacon (FE95) parser + AES-CCM decryption.
# Works on MicroPython (cryptolib) and CPython (cryptography) so it can be unit-tested on a PC.
import struct
import binascii

try:
    from cryptolib import aes as _aes

    def _ecb(key):
        return _aes(key, 1).encrypt
except ImportError:  # CPython
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def _ecb(key):
        return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update


UUID_FE95 = 0xFE95
OBJ_S400 = 0x6E16  # MIoT event 11.1022 (customized-event-1)
S400_PIDS = (0x30D9, 0x3BD5, 0x48CF, 0x4B05)


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def aes_ccm_decrypt(key, nonce, ct, tag, aad=b""):
    """AES-CCM decrypt+verify built on AES-ECB. Returns plaintext or None if the tag is wrong."""
    L = 15 - len(nonce)
    M = len(tag)
    E = _ecb(key)
    s0 = E(bytes([L - 1]) + nonce + (0).to_bytes(L, "big"))
    pt = bytearray(len(ct))
    for blk in range((len(ct) + 15) // 16):
        s = E(bytes([L - 1]) + nonce + (blk + 1).to_bytes(L, "big"))
        base = blk * 16
        for j in range(min(16, len(ct) - base)):
            pt[base + j] = ct[base + j] ^ s[j]
    flags = (0x40 if aad else 0) | (((M - 2) // 2) << 3) | (L - 1)
    b = bytearray(bytes([flags]) + nonce + len(ct).to_bytes(L, "big"))
    if aad:
        a = len(aad).to_bytes(2, "big") + aad
        b += a + bytes((-len(a)) % 16)
    b += pt + bytes((-len(pt)) % 16)
    x = bytes(16)
    for off in range(0, len(b), 16):
        x = E(_xor(x, b[off:off + 16]))
    if _xor(x[:M], s0[:M]) != bytes(tag):
        return None
    return bytes(pt)


def iter_adv(adv):
    """Yield (ad_type, value) from raw advertising data."""
    i = 0
    n = len(adv)
    while i + 1 < n:
        ln = adv[i]
        if ln == 0 or i + 1 + ln > n:
            return
        yield adv[i + 1], adv[i + 2:i + 1 + ln]
        i += ln + 1


def find_fe95(adv):
    for t, v in iter_adv(adv):
        if t == 0x16 and len(v) >= 2 and v[0] == 0x95 and v[1] == 0xFE:
            return bytes(v[2:])
    return None


def find_name(adv):
    for t, v in iter_adv(adv):
        if t in (0x08, 0x09):
            try:
                return bytes(v).decode()
            except Exception:
                return None
    return None


def parse(sd, mac, key=None):
    """Parse FE95 service data.

    sd:  service data without the UUID
    mac: 6 bytes, normal (big-endian) order as reported by the BLE stack
    key: 16-byte bindkey or None
    Returns dict or None. dict["objects"] is a list of (obj_id, bytes); dict["error"] is set on failures.
    """
    if len(sd) < 5:
        return None
    frctrl = sd[0] | (sd[1] << 8)
    version = frctrl >> 12
    if frctrl & 0x80 or version < 2:  # mesh or very old format
        return None
    res = {
        "frctrl": frctrl,
        "version": version,
        "pid": sd[2] | (sd[3] << 8),
        "cnt": sd[4],
        "registered": bool(frctrl & 0x100),
        "encrypted": bool(frctrl & 0x08),
        "objects": [],
        "error": None,
    }
    i = 5
    mac_rev = bytes(mac[5 - k] for k in range(6))  # no step slices on MicroPython rp2
    if frctrl & 0x10:
        if len(sd) < i + 6:
            return None
        if bytes(sd[5:11]) != mac_rev:
            res["error"] = "mac mismatch"
            return res
        i += 6
    if frctrl & 0x20:
        if len(sd) < i + 1:
            return None
        cap = sd[i]
        i += 1
        if cap & 0x20:
            i += 1
    if not frctrl & 0x40:
        return res  # no object (idle beacon)
    if res["encrypted"]:
        if version < 4:
            res["error"] = "legacy encryption unsupported"
            return res
        if len(sd) < i + 3 + 7:
            res["error"] = "short"
            return res
        res["ext_cnt"] = bytes(sd[-7:-4])
        if key is None:
            res["error"] = "no key"
            return res
        nonce = mac_rev + bytes(sd[2:5]) + bytes(sd[-7:-4])
        payload = aes_ccm_decrypt(key, nonce, bytes(sd[i:-7]), bytes(sd[-4:]), b"\x11")
        if payload is None:
            res["error"] = "bad key/mic"
            return res
    else:
        payload = bytes(sd[i:])
    j = 0
    while j + 3 <= len(payload):
        oid = payload[j] | (payload[j + 1] << 8)
        ln = payload[j + 2]
        if j + 3 + ln > len(payload):
            break
        res["objects"].append((oid, payload[j + 3:j + 3 + ln]))
        j += 3 + ln
    return res


def decode_s400(obj):
    """Decode object 0x6E16 of the Xiaomi Body Composition Scale S400.

    mass==0 and hr==0 and imp==0 -> user stepped off
    mass!=0 and imp!=0          -> weight + impedance at 50 kHz (not final)
    mass==0 and hr==0 and imp!=0 -> impedance at 250 kHz (final packet)
    mass!=0 and imp==0          -> weight only (e.g. with socks), final
    """
    if len(obj) < 5:
        return None
    profile, data = struct.unpack("<BI", obj[:5])
    extra = struct.unpack("<I", obj[5:9])[0] if len(obj) >= 9 else None
    hr = (data >> 11) & 0x7F
    return {
        "profile": profile,
        "raw": data,
        "extra": extra,
        "weight": (data & 0x7FF) / 10,
        "hr": hr + 50 if 0 < hr < 127 else 0,
        "imp": (data >> 18) / 10,
    }


def hexs(b):
    return binascii.hexlify(bytes(b)).decode()
