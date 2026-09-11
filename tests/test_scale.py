import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico"))
sys.path.insert(0, os.path.dirname(__file__))
import mibeacon  # noqa: E402
import scale  # noqa: E402
from test_mibeacon import MAC, build_frame  # noqa: E402

KEY = os.urandom(16)


def s400(profile, weight=0.0, hr=0, imp=0.0, cnt=1, ts=0):
    data = int(round(weight * 10)) | ((hr - 50 if hr else 0) << 11) | (int(round(imp * 10)) << 18)
    obj = struct.pack("<BII", profile, data, ts)
    return build_frame(KEY, mibeacon.OBJ_S400, obj, cnt=cnt, ext=bytes([cnt, 0, 0]))


class TestTracker(unittest.TestCase):
    def test_full_weighing(self):
        t = scale.ScaleTracker(MAC, KEY)
        for _ in range(5):  # repeated advert with the same counter -> one object
            t.feed(s400(1, 80.3, 71, 480.5, cnt=10), 1000)
        self.assertEqual(len(t.cur.records), 1)
        t.feed(s400(1, imp=455.2, cnt=11), 1002)
        done = t.pop_done()
        self.assertEqual(len(done), 1)
        m = done[0]
        self.assertEqual((m.weight, m.hr, m.imp_low, m.imp_high, m.complete), (80.3, 71, 480.5, 455.2, True))
        self.assertEqual(len(m.records), 2)
        self.assertIsNone(t.cur)

    def test_weight_only_finishes_on_step_off(self):
        t = scale.ScaleTracker(MAC, KEY)
        t.feed(s400(2, 65.0, cnt=20), 50)
        t.feed(s400(2, cnt=21), 55)  # all zeros: stepped off
        done = t.pop_done()
        self.assertEqual([(m.weight, m.complete) for m in done], [(65.0, False)])

    def test_idle_timeout(self):
        t = scale.ScaleTracker(MAC, KEY)
        t.feed(s400(1, 70.0, cnt=30), 100)
        t.poll(100 + scale.IDLE_FINISH_S - 1)
        self.assertEqual(t.pop_done(), [])
        t.poll(100 + scale.IDLE_FINISH_S)
        self.assertEqual(len(t.pop_done()), 1)

    def test_scale_timestamp_used(self):
        t = scale.ScaleTracker(MAC, KEY)
        t.feed(s400(1, 71.0, 72, 474.9, cnt=40, ts=1789147103), 1789147110)
        t.feed(s400(1, imp=423.1, cnt=41, ts=1789147103), 1789147111)
        d = t.pop_done()[0].to_dict()
        self.assertEqual((d["ts"], d["t"]), (1789147103, 1789147110))

    def test_wrong_key_reports_error(self):
        t = scale.ScaleTracker(MAC, os.urandom(16))
        self.assertEqual(t.feed(s400(1, 70.0), 1), [])
        self.assertEqual(t.last_error, "bad key/mic")


if __name__ == "__main__":
    unittest.main()
