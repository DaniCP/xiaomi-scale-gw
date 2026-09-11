import os
import struct
import sys
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico"))
import mibeacon  # noqa: E402

MAC = bytes.fromhex("aabbccddeeff")


def build_frame(key, obj_id, obj, pid=0x3BD5, cnt=0x21, ext=b"\x01\x02\x03"):
    frctrl = 0x5000 | 0x100 | 0x40 | 0x10 | 0x08
    head = struct.pack("<HHB", frctrl, pid, cnt) + MAC[::-1]
    nonce = MAC[::-1] + struct.pack("<HB", pid, cnt) + ext
    payload = struct.pack("<HB", obj_id, len(obj)) + obj
    enc = AESCCM(key, tag_length=4).encrypt(nonce, payload, b"\x11")
    return head + enc[:-4] + ext + enc[-4:]


class TestCCM(unittest.TestCase):
    def test_random_vectors(self):
        for n in range(200):
            key, nonce = os.urandom(16), os.urandom(12)
            pt = os.urandom(n % 40)
            aad = b"\x11" if n % 2 else b""
            enc = AESCCM(key, tag_length=4).encrypt(nonce, pt, aad or None)
            self.assertEqual(mibeacon.aes_ccm_decrypt(key, nonce, enc[:-4], enc[-4:], aad), pt)
            bad = bytes([enc[-1] ^ 1])
            self.assertIsNone(mibeacon.aes_ccm_decrypt(key, nonce, enc[:-4], enc[-4:-1] + bad, aad))


class TestParse(unittest.TestCase):
    def test_idle_beacon(self):
        # S400 idle advert: frame control 0x5910 (v5, registered, MAC included, no object)
        r = mibeacon.parse(bytes.fromhex("1059d53b14ffeeddccbbaa"), MAC)
        self.assertEqual(r["pid"], 0x3BD5)
        self.assertEqual(r["version"], 5)
        self.assertTrue(r["registered"])
        self.assertEqual(r["objects"], [])
        self.assertIsNone(r["error"])

    def test_encrypted_s400_object(self):
        key = os.urandom(16)
        # 72.4 kg, hr 68 bpm, impedance 512.3 ohm
        data = 724 | ((68 - 50) << 11) | (5123 << 18)
        obj = struct.pack("<BII", 1, data, 0)
        r = mibeacon.parse(build_frame(key, mibeacon.OBJ_S400, obj), MAC, key)
        self.assertIsNone(r["error"])
        self.assertEqual(r["objects"], [(mibeacon.OBJ_S400, obj)])
        d = mibeacon.decode_s400(obj)
        self.assertEqual((d["profile"], d["weight"], d["hr"], d["imp"]), (1, 72.4, 68, 512.3))

    def test_wrong_key(self):
        frame = build_frame(os.urandom(16), mibeacon.OBJ_S400, bytes(9))
        self.assertEqual(mibeacon.parse(frame, MAC, os.urandom(16))["error"], "bad key/mic")


if __name__ == "__main__":
    unittest.main()
