import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico"))
import uploader  # noqa: E402

CFG = {"scale": {"did": "blt.4.x", "model": "yunmai.scales.ms104", "sn": "SN-EXAMPLE"}}
USER = {"uid": "1234567890", "accountId": "1234567890", "type": 1, "sex": "1", "height": "173",
        "birth": "644191200000", "accountCode": 1, "weightUpdateTime": 1788498404000}
# sample /eco/scale/bcalc answer
BCALC = {"bfp": 19.8, "slm": 53.8, "bwp": 58.7, "bmc": 3.1, "vfl": 6, "pp": 16.2, "smm": 30.5, "bmi": 23.7,
         "swt": 65.1, "mc": -1.6, "wc": -5.9, "fc": -4.3, "whr": 0.8, "bmr": 1599, "bt": 0, "ma": 34, "sbc": 81,
         "slp": 75.8, "bmcp": 4.4, "bfm": 14.1, "ffm": 56.9, "bwm": 41.7, "pm": 11.5, "smi": 10.2}
WEIGHING = {"t": 1789147104, "ts": 1789147103, "profile": 1, "weight": 71.0, "hr": 72,
            "imp_low": 474.9, "imp_high": 423.1, "complete": True, "records": []}


class FakeCloud:
    user_id = "1234567890"

    def __init__(self, users=(USER,), bcalc=BCALC):
        self.calls = []
        self.users = list(users)
        self.bcalc = bcalc

    def api(self, path, params, headers=None):
        self.calls.append((path, params, headers))
        if path == "/eco/scale/account/list":
            return {"code": 0, "result": self.users}
        if path == "/eco/scale/bcalc":
            return {"code": 0, "result": self.bcalc} if self.bcalc else {"code": -1, "message": "x"}
        return {"code": 0, "result": True}

    def paths(self):
        return [c[0] for c in self.calls]

    def params(self, path):
        return next(c[1] for c in self.calls if c[0] == path)


class TestUploader(unittest.TestCase):
    def test_recognised_user_full_composition(self):
        c = FakeCloud()
        ok, info = uploader.upload(c, CFG, dict(WEIGHING))
        self.assertTrue(ok)
        self.assertEqual(c.paths(), ["/eco/scale/account/list", "/eco/scale/bcalc", "/eco/common/scale/add",
                                     "/eco/scale/account/updateWeight"])
        self.assertEqual(c.params("/eco/scale/bcalc"), {
            "version": "235", "age": 36, "sex": 1, "height": 1730, "weight": 710, "electrodes_number": 4,
            "res_mapping_switch": 0, "current_impedance": [4749, 4231]})
        rec = c.params("/eco/common/scale/add")
        self.assertEqual({k: v for k, v in rec.items() if k != "data"}, {
            "model": "yunmai.scales.ms104", "uid": 1234567890, "accountId": 1234567890, "did": "blt.4.x",
            "createTime": 1789147103000, "dataVersion": 1, "sn": "SN-EXAMPLE", "fromSource": 2})
        data = json.loads(rec["data"])
        # same values/types the official app stores
        for k, v in {"miid": "1234567890", "duid": "1", "userType": "1", "weight": 71.0, "heartRate": "72",
                     "status": "0", "time": "1789147103", "bfp": 19.8, "vfl": "6", "bmr": "1599", "bt": "0",
                     "ma": "34", "sbc": "81", "bodyRes": 474.9, "bodyRes2": 423.1, "bmi": 23.7}.items():
            self.assertEqual(data[k], v, k)
        self.assertEqual(data["user"]["accountCode"], 1)
        self.assertEqual(c.params("/eco/scale/account/updateWeight"), {
            "uid": 1234567890, "weightTarget": 71.0, "accountId": 1234567890, "weightUpdateTime": 1789147103000})
        self.assertTrue(all(h == {"MIOT-REQUEST-MODEL": "yunmai.scales.ms104"} for _, _, h in c.calls))

    def test_weight_only_skips_bcalc_and_computes_bmi(self):
        c = FakeCloud()
        m = dict(WEIGHING, imp_low=0, imp_high=0, hr=0)
        uploader.upload(c, CFG, m)
        self.assertNotIn("/eco/scale/bcalc", c.paths())
        data = json.loads(c.params("/eco/common/scale/add")["data"])
        self.assertEqual((data["bmi"], data["bfp"], data["vfl"]), (23.7, 0, "0"))

    def test_bcalc_failure_still_saves_weight(self):
        c = FakeCloud(bcalc=None)
        ok, info = uploader.upload(c, CFG, dict(WEIGHING))
        self.assertTrue(ok)
        self.assertFalse(info["composition"])
        self.assertIn("/eco/common/scale/add", c.paths())

    def test_unknown_profile_goes_to_unclaimed(self):
        c = FakeCloud()
        ok, info = uploader.upload(c, CFG, dict(WEIGHING, profile=0))
        self.assertTrue(info["unclaimed"])
        self.assertEqual(c.paths(), ["/eco/scale/account/list", "/eco/common/scale/batchAddClaim"])
        rec = c.params("/eco/common/scale/batchAddClaim")[0]
        self.assertEqual((rec["uid"], rec["accountId"], rec["createTime"]), (0, 0, 1789147103000))

    def test_cloud_error_raises(self):
        class Bad(FakeCloud):
            def api(self, path, params, headers=None):
                return {"code": -8, "message": "bad"} if path.endswith("/add") else super().api(path, params, headers)
        with self.assertRaises(uploader.UploadError):
            uploader.upload(Bad(), CFG, dict(WEIGHING))


if __name__ == "__main__":
    unittest.main()
