# Groups S400 MiBeacon objects into complete weighings.
import mibeacon

IDLE_FINISH_S = 12  # close a weighing this many seconds after the last new object
SEEN_MAX = 48


class Measurement:
    def __init__(self, t):
        self.t0 = t
        self.t_last = t
        self.records = []  # [obj_id, hex, unix_ts]
        self.scale_ts = None  # measurement time from the scale's own clock (object "extra" field)
        self.profile = None
        self.weight = 0
        self.hr = 0
        self.imp_low = 0
        self.imp_high = 0
        self.complete = False

    def to_dict(self):
        return {
            "t": self.t0, "ts": self.scale_ts or self.t0, "profile": self.profile, "weight": self.weight, "hr": self.hr,
            "imp_low": self.imp_low, "imp_high": self.imp_high, "complete": self.complete,
            "records": self.records,
        }


class ScaleTracker:
    def __init__(self, mac, key):
        self.mac = mac
        self.key = key
        self.seen = []
        self.cur = None
        self.done = []
        self.last_error = None

    def feed(self, sd, now):
        """Feed FE95 service data from the scale. Returns list of decoded new S400 objects."""
        r = mibeacon.parse(sd, self.mac, self.key)
        if r is None:
            return []
        self.last_error = r["error"]
        new = []
        for oid, obj in r["objects"]:
            if oid != mibeacon.OBJ_S400:
                continue
            ident = (r["cnt"], r.get("ext_cnt"), bytes(obj))
            if ident in self.seen:
                continue
            self.seen.append(ident)
            if len(self.seen) > SEEN_MAX:
                self.seen.pop(0)
            d = mibeacon.decode_s400(obj)
            if d is None:
                continue
            d["hex"] = mibeacon.hexs(obj)
            self._add(oid, d, now)
            new.append(d)
        return new

    def _add(self, oid, d, now):
        w, hr, imp = d["weight"], d["hr"], d["imp"]
        if w == 0 and hr == 0 and imp == 0:  # stepped off
            if self.cur:
                self._finish()
            return
        m = self.cur
        if m is None:
            m = self.cur = Measurement(now)
        m.t_last = now
        m.records.append([oid, d["hex"], now])
        m.profile = d["profile"]
        if d["extra"] and d["extra"] > 1600000000:
            m.scale_ts = d["extra"]
        if w > 0:
            m.weight = w
        if hr > 0:
            m.hr = hr
        if w > 0 and imp > 0:
            m.imp_low = imp
        if w == 0 and hr == 0 and imp > 0:  # 250 kHz impedance: last packet of a weighing
            m.imp_high = imp
            m.complete = True
            self._finish()

    def poll(self, now):
        if self.cur and now - self.cur.t_last >= IDLE_FINISH_S:
            self._finish()

    def _finish(self):
        if self.cur.weight > 0:
            self.done.append(self.cur)
        self.cur = None

    def pop_done(self):
        out, self.done = self.done, []
        return out
