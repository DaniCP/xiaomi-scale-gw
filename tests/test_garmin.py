import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico"))
import garmin  # noqa: E402
import uploader  # noqa: E402
from test_uploader import CFG, WEIGHING, FakeCloud  # noqa: E402

CLIENT_ID = "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2"
W = {"ts": 1789147103, "weight": 71.0, "bfp": 19.8, "bwp": 58.7, "bmc": 3.1, "smm": 30.5, "bmr": "1599",
     "ma": "34", "vfl": "6", "bmi": 23.7}
# python-garminconnect 0.3.20 FitEncoderWeight output for W (file_info, file_creator, device_info, weight_scale)
FIT_W = bytes.fromhex(
    "0c106c00c60000002e46495440000000000603048c0404860102840202840502840001000000000000dfeb0645ffffffffffff0941"
    "000031000200028401010201ffffff42000017000cfd048603048c0704860804860202840402840502840a02840001020101020601"
    "020b010202dfeb064500000000ffffffffffffffffffffffffffffffffffffffff4300001e000dfd04860002840102840202840302"
    "840402840502840702840902840801020a01020b01020d028403dfeb0645bc1bbc07ee16ffff3601ea0bfc18ffffff2206ed002467")


def fake_jwt(exp, client_id=CLIENT_ID):
    def enc(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    return "%s.%s.c2ln" % (enc({"alg": "RS256", "typ": "JWT"}), enc({"exp": exp, "client_id": client_id}))


class FakeHttp:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers=None, body=None, timeout=30):
        self.calls.append((method, url, headers, body))
        st, j = self.answers.pop(0)
        return st, [], json.dumps(j).encode()


class TestFit(unittest.TestCase):
    def test_matches_reference_encoder(self):
        self.assertEqual(garmin.fit_weight(W), FIT_W)

    def test_crc_and_sizes(self):
        fit = garmin.fit_weight({"ts": 1789147103, "weight": 80.25})
        self.assertEqual(fit[8:12], b".FIT")
        self.assertEqual(int.from_bytes(fit[4:8], "little"), len(fit) - 14)
        self.assertEqual(garmin.fit_crc(fit), 0)  # CRC over data + its CRC is zero

    def test_unmeasured_fields_are_invalid(self):
        fit = garmin.fit_weight({"ts": 1789147103, "weight": 71.0, "bfp": 0, "vfl": "0"})
        rec = fit[-28:-2]  # weight_scale data record
        self.assertEqual(rec[5:7], (7100).to_bytes(2, "little"))
        self.assertEqual(rec[7:9], b"\xff\xff")  # percent_fat
        self.assertEqual(rec[23], 0xFF)  # visceral_fat_rating


class TestFromXiaomi(unittest.TestCase):
    def test_full_composition(self):
        m = dict(WEIGHING)
        ok, info = uploader.upload(FakeCloud(), CFG, m)
        w = garmin.from_xiaomi(m, info["data"])
        self.assertEqual(w, {"ts": 1789147103, "weight": 71.0, "bfp": 19.8, "bwp": 58.7, "bmc": 3.1, "smm": 30.5,
                             "bmr": "1599", "ma": "34", "vfl": "6", "bmi": 23.7})

    def test_failed_composition_keeps_weight_and_bmi(self):
        m = dict(WEIGHING)
        ok, info = uploader.upload(FakeCloud(bcalc=None), CFG, m)
        w = garmin.from_xiaomi(m, info["data"])
        self.assertEqual(w["weight"], 71.0)
        self.assertEqual(w["bmi"], 23.7)
        self.assertEqual(garmin.fit_weight(w)[-28:-2][7:9], b"\xff\xff")  # "0" values are not sent


class TestClient(unittest.TestCase):
    def setUp(self):
        self._http = garmin.http_request

    def tearDown(self):
        garmin.http_request = self._http

    def test_expires_in(self):
        g = garmin.Garmin(fake_jwt(1000 + 3600), "r", CLIENT_ID)
        self.assertEqual(g.expires_in(1000), 3600)
        self.assertEqual(garmin.Garmin("not-a-jwt", "r", CLIENT_ID).expires_in(1000), 0)

    def test_refresh_rotates_tokens(self):
        new = fake_jwt(2000000000)
        garmin.http_request = http = FakeHttp((200, {"access_token": new, "refresh_token": "r2"}))
        g = garmin.Garmin(fake_jwt(1), "r1+/=", CLIENT_ID)
        saved = []
        g.on_tokens = lambda c: saved.append((c.access_token, c.refresh_token, c.client_id))
        g.refresh()
        method, url, headers, body = http.calls[0]
        self.assertEqual((method, url), ("POST", garmin.DI_TOKEN_URL))
        self.assertEqual(body, "grant_type=refresh_token&client_id=%s&refresh_token=r1%%2B%%2F%%3D" % CLIENT_ID)
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode((CLIENT_ID + ":").encode()).decode())
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(saved, [(new, "r2", CLIENT_ID)])

    def test_refresh_rejected(self):
        garmin.http_request = FakeHttp((400, {"error": "invalid_grant"}))
        with self.assertRaises(garmin.AuthError):
            garmin.Garmin(fake_jwt(1), "r", CLIENT_ID).refresh()

    def test_upload(self):
        garmin.http_request = http = FakeHttp((202, {"detailedImportResult": {"uploadId": 42, "failures": []}}))
        g = garmin.Garmin(fake_jwt(2000000000), "r", CLIENT_ID)
        self.assertEqual(g.upload_weight(W), {"http": 202, "id": 42})
        method, url, headers, body = http.calls[0]
        self.assertEqual((method, url), ("POST", garmin.UPLOAD_URL))
        self.assertEqual(headers["Authorization"], "Bearer " + g.access_token)
        boundary = headers["Content-Type"].split("boundary=")[1]
        self.assertTrue(body.startswith(("--%s\r\n" % boundary).encode()))
        self.assertIn(b'filename="weight.fit"', body)
        self.assertIn(FIT_W, body)
        self.assertTrue(body.endswith(("\r\n--%s--\r\n" % boundary).encode()))

    def test_upload_renews_expired_token_first(self):
        new = fake_jwt(2000000000)
        garmin.http_request = http = FakeHttp((200, {"access_token": new, "refresh_token": "r2"}), (201, {}))
        g = garmin.Garmin(fake_jwt(1), "r", CLIENT_ID)
        g.upload_weight(W)
        self.assertEqual([c[1] for c in http.calls], [garmin.DI_TOKEN_URL, garmin.UPLOAD_URL])
        self.assertEqual(http.calls[1][2]["Authorization"], "Bearer " + new)

    def test_upload_401_renews_and_retries(self):
        garmin.http_request = http = FakeHttp(
            (401, {}), (200, {"access_token": fake_jwt(2000000001), "refresh_token": "r2"}), (201, {}))
        garmin.Garmin(fake_jwt(2000000000), "r", CLIENT_ID).upload_weight(W)
        self.assertEqual([c[1] for c in http.calls], [garmin.UPLOAD_URL, garmin.DI_TOKEN_URL, garmin.UPLOAD_URL])

    def test_upload_duplicate_and_errors(self):
        g = garmin.Garmin(fake_jwt(2000000000), "r", CLIENT_ID)
        garmin.http_request = FakeHttp((409, {}))
        self.assertEqual(g.upload_weight(W), {"duplicate": True})
        garmin.http_request = FakeHttp((500, {}))
        with self.assertRaises(garmin.GarminError):
            g.upload_weight(W)
        garmin.http_request = FakeHttp((202, {"detailedImportResult": {"failures": [{"messages": ["bad"]}]}}))
        with self.assertRaises(garmin.GarminError):
            g.upload_weight(W)

    def test_seed_id(self):
        self.assertEqual(garmin.seed_id("abc"), garmin.seed_id("abc"))
        self.assertNotEqual(garmin.seed_id("abc"), garmin.seed_id("abd"))
        self.assertNotIn("abc", garmin.seed_id("abc"))


if __name__ == "__main__":
    unittest.main()
