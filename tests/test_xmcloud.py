import base64
import hashlib
import json
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote as ref_quote

from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
from cryptography.hazmat.primitives.ciphers import Cipher

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico"))
import xmcloud  # noqa: E402


def ref_rc4(key, data):
    enc = Cipher(ARC4(key), mode=None).encryptor()
    enc.update(bytes(1024))
    return enc.update(data)


def ref_sig(method, path, params, signed_nonce_b64):
    # Same algorithm as Xiaomi-cloud-tokens-extractor generate_enc_signature
    parts = [method, path] + ["%s=%s" % kv for kv in params] + [signed_nonce_b64]
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


class TestPrimitives(unittest.TestCase):
    def test_rc4(self):
        for n in (0, 1, 17, 300):
            key, data = os.urandom(32), os.urandom(n)
            self.assertEqual(xmcloud.rc4(key, data), ref_rc4(key, data))

    def test_quote(self):
        s = "aZ09-_.~ +/=&%ñ"
        self.assertEqual(xmcloud.quote(s), ref_quote(s, safe="-_.~"))


class TestApiSigning(unittest.TestCase):
    def test_request_and_response(self):
        ssecurity = base64.b64encode(os.urandom(16)).decode()
        cloud = xmcloud.MiCloud("de", 123, "pt", ssecurity, "stoken", "dev")
        nonce8 = b"\x01" * 8
        captured = {}

        def fake_http(method, url, headers=None, body=None, timeout=30):
            captured.update(method=method, url=url, headers=headers, body=body)
            nonce = base64.b64decode(parse_qs(body)["_nonce"][0])
            snonce = hashlib.sha256(base64.b64decode(ssecurity) + nonce).digest()
            resp = base64.b64encode(ref_rc4(snonce, b'{"code":0,"message":"ok","result":"done"}'))
            return 200, [], resp

        params = [{"did": "blt.3.x", "type": "event", "key": "28182", "value": "[\"00\"]", "time": 1}]
        with mock.patch.object(xmcloud, "http_request", fake_http), \
                mock.patch.object(xmcloud.os, "urandom", return_value=nonce8), \
                mock.patch.object(xmcloud.time, "time", return_value=1700000000):
            res = cloud.set_user_device_data(params)

        self.assertEqual(res["result"], "done")
        self.assertEqual(captured["url"], "https://de.api.io.mi.com/app/user/set_user_device_data")
        self.assertIn("serviceToken=stoken", captured["headers"]["Cookie"])

        form = {k: v[0] for k, v in parse_qs(captured["body"]).items()}
        nonce = nonce8 + (1700000000 // 60).to_bytes(4, "big")
        self.assertEqual(base64.b64decode(form["_nonce"]), nonce)
        snonce = hashlib.sha256(base64.b64decode(ssecurity) + nonce).digest()
        sn64 = base64.b64encode(snonce).decode()
        data = json.dumps(params)
        path = "/user/set_user_device_data"
        exp_hash = ref_sig("POST", path, [("data", data)], sn64)
        self.assertEqual(ref_rc4(snonce, base64.b64decode(form["data"])).decode(), data)
        self.assertEqual(ref_rc4(snonce, base64.b64decode(form["rc4_hash__"])).decode(), exp_hash)
        exp_sig = ref_sig("POST", path, [("data", form["data"]), ("rc4_hash__", form["rc4_hash__"])], sn64)
        self.assertEqual(form["signature"], exp_sig)


if __name__ == "__main__":
    unittest.main()
