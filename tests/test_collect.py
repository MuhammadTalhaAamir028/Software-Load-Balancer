"""Tests for bench/collect.py and bench/analyze.py on small synthetic runs with known answers."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bench"))

import analyze  # noqa: E402
import collect  # noqa: E402

T0 = 1_791_913_800_000      # epoch ms
KILL = T0 + 30_100          # the kill happened 100 ms after t0 + 30 s
DOWN = KILL + 2_500         # detection 2.5 s later
HEADER = "response-time,DNS+dialup,DNS,Request-write,Response-delay,Response-read,status-code,offset\n"


def iso(ms, digits=3):
    base = datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return base + "." + ("%03d" % (ms % 1000)).ljust(digits, "0") + "Z"


def hey_csv(path, failed_offsets=()):
    """10 pre rows (1..10 ms at offsets 1..10) and 10 post rows (11..20 ms at 30.5..39.5)."""
    with open(path, "w") as fh:
        fh.write(HEADER)
        for i in range(10):
            fh.write("%.4f,0,0,0,0,0,200,%.4f\n" % ((i + 1) / 1000.0, i + 1))
        for i in range(10):
            off = 30.5 + i
            status = 502 if off in failed_offsets else 200
            fh.write("%.4f,0,0,0,0,0,%d,%.4f\n" % ((i + 11) / 1000.0, status, off))


def stats_json(path, counts):
    with open(path, "w") as fh:
        json.dump({"per_backend": {b: {"requests": n} for b, n in counts.items()}}, fh)


def make_run(d, system="own", failed=(30.5, 31.5), failed_c2=(33.5,)):
    for i in range(1, 5):
        hey_csv(os.path.join(d, "hey_c%d.csv" % i),
                failed if i == 1 else failed_c2 if i == 2 else ())
    for name, val in (("t0.txt", T0), ("kill_ts.txt", KILL), ("ulimit.txt", 1048576)):
        with open(os.path.join(d, name), "w") as fh:
            fh.write("%d\n" % val)
    snaps = {"s0": dict(b1=0, b2=0, b3=0, b4=0), "s30": dict(b1=10, b2=10, b3=10, b4=10),
             "s60": dict(b1=25, b2=25, b3=12, b4=25)}
    for name, counts in snaps.items():
        if system == "own":
            stats_json(os.path.join(d, "stats_%s.json" % name), counts)
        else:
            with open(os.path.join(d, "stats_%s.csv" % name), "w") as fh:
                fh.write("# pxname,svname,qcur,stot,status\n")
                fh.write("fe,FRONTEND,,999,OPEN\nbe,BACKEND,,999,UP\n")
                for b, n in counts.items():
                    fh.write("be,%s,0,%d,UP\n" % (b, n))
    if system == "own":
        with open(os.path.join(d, "lb.jsonl"), "w") as fh:
            for ev in (
                {"ts": iso(T0 - 20_000), "event": "backend_down", "backend": "b3"},   # before the kill
                {"ts": iso(DOWN - 1), "event": "backend_down", "backend": "b1"},      # other backend
                {"ts": iso(DOWN), "event": "backend_down", "backend": "b3"},
            ):
                fh.write(json.dumps(ev) + "\n")
    else:
        with open(os.path.join(d, "haproxy.log"), "w") as fh:
            fh.write("%s Server be/b1 is DOWN, reason: x\n" % iso(KILL - 5_000, 9))
            fh.write("%s Server be/b3 is DOWN, reason: Layer4 timeout\n" % iso(DOWN, 9))
    with open(os.path.join(d, "cpu.csv"), "w") as fh:
        fh.write("t_s,cpu_pct\n")
        for t in range(1, 61):
            fh.write("%d,%s\n" % (t, 95.0 if t == 45 else 50.0 if t <= 30 else 70.0))
    with open(os.path.join(d, "cpu_clients.csv"), "w") as fh:
        fh.write("t_s,c1,c2,c3,c4\n")
        for t in range(1, 61):
            fh.write("%d,40,40,40,40\n" % t)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def summary(self, **kw):
        return collect.build_summary(self.dir, "exp1", kw.pop("system", "own"), "round_robin", 200, 0, 1, **kw)

    def test_percentile_nearest_rank(self):
        vals = list(range(1, 101))
        self.assertEqual([collect.percentile(vals, p) for p in (50, 95, 99)], [50, 95, 99])
        self.assertEqual(collect.percentile([], 50), 0.0)

    def test_windows_failed_and_detection(self):
        make_run(self.dir)
        s = self.summary()
        pre, post = s["windows"]["pre"], s["windows"]["post"]
        self.assertEqual((pre["requests"], post["requests"]), (40, 40))
        self.assertAlmostEqual(pre["rps"], 40 / 30, places=2)
        # 40 values: 1..10 ms four times each -> nearest rank 20 -> 5, 38 -> 10, 40 -> 10
        self.assertEqual((pre["p50_ms"], pre["p95_ms"], pre["p99_ms"]), (5.0, 10.0, 10.0))
        self.assertEqual((post["p50_ms"], post["p95_ms"]), (15.0, 20.0))
        self.assertEqual((pre["failed"], post["failed"]), (0, 3))
        self.assertEqual(s["detection_s"], 2.5)
        self.assertEqual(s["failed_in_detection_window"], 2)   # the 502 at t0+33.5 s is after DOWN
        self.assertEqual(s["per_backend_pre"], {"b1": 10, "b2": 10, "b3": 10, "b4": 10})
        self.assertEqual(s["per_backend_post"], {"b1": 15, "b2": 15, "b3": 2, "b4": 15})
        self.assertEqual(s["cpu_pct"], {"pre_mean": 50.0, "post_mean": 70.8, "peak": 95.0})
        self.assertTrue(s["valid"], s["invalid_reason"])

    def test_haproxy_inputs(self):
        make_run(self.dir, system="haproxy")
        s = self.summary(system="haproxy")
        self.assertEqual(s["detection_s"], 2.5)
        self.assertEqual(s["per_backend_post"]["b3"], 2)
        self.assertTrue(s["valid"], s["invalid_reason"])

    def test_invalid_runs_are_marked(self):
        def put(name, text, mode="w"):
            with open(os.path.join(self.dir, name), mode) as fh:
                fh.write(text)

        cases = {
            "kill_ts.txt missing": lambda: os.remove(os.path.join(self.dir, "kill_ts.txt")),
            "ulimit": lambda: put("ulimit.txt", "1024\n"),
            "expected 4 hey csv": lambda: os.remove(os.path.join(self.dir, "hey_c4.csv")),
            "before the kill": lambda: put("hey_c3.csv", "0.5,0,0,0,0,0,502,5.0\n", "a"),
            "no backend_down": lambda: put("lb.jsonl", ""),
            "exceeded 90%": lambda: put("cpu_clients.csv", "t_s,c1,c2,c3,c4\n"
                                        + "".join("%d,95,1,1,1\n" % t for t in range(1, 10))),
        }
        for expected, break_it in cases.items():
            with self.subTest(expected):
                shutil.rmtree(self.dir)
                os.makedirs(self.dir)
                make_run(self.dir)
                break_it()
                s = self.summary()
                self.assertFalse(s["valid"])
                self.assertIn(expected, s["invalid_reason"])

    def test_late_kill_is_invalid(self):
        make_run(self.dir)
        with open(os.path.join(self.dir, "kill_ts.txt"), "w") as fh:
            fh.write("%d\n" % (T0 + 31_000))
        self.assertIn("not t0+30 s", self.summary()["invalid_reason"])

    def test_hey_columns_are_found_by_name(self):
        path = os.path.join(self.dir, "x.csv")
        with open(path, "w") as fh:
            fh.write("offset,status-code,response-time\n1.5,200,0.020\n")
        self.assertEqual(collect.read_hey_csv(path), [(1.5, 20.0, 200)])

    def test_docker_timestamp_with_nanoseconds(self):
        self.assertEqual(collect.iso_to_ms("2026-10-13T17:50:27.560123456Z"),
                         collect.iso_to_ms("2026-10-13T17:50:27.560Z"))


class AnalyzeTest(unittest.TestCase):
    def test_mean_and_sample_std_over_runs(self):
        runs = []
        for run, rps in enumerate((10.0, 20.0, 30.0), 1):
            w = {"rps": rps, "p50_ms": 1, "p95_ms": 2, "p99_ms": 3, "requests": 100, "failed": 0}
            runs.append({"exp": "exp1", "system": "own", "algorithm": "round_robin", "concurrency": 50,
                         "rtt_ms": 0, "run": run, "windows": {"pre": w, "post": w}, "detection_s": 2.0,
                         "failed_in_detection_window": 0, "cpu_pct": {"pre_mean": 1, "post_mean": 1, "peak": 2},
                         "per_backend_pre": {"b1": 5}, "per_backend_post": {"b1": 5}, "valid": True})
        agg = analyze.aggregate(runs)
        a = agg[("exp1", "own", "round_robin", 50, 0)]
        self.assertEqual(a["n"], 3)
        self.assertEqual((a["pre_rps"]["mean"], a["pre_rps"]["std"]), (20.0, 10.0))
        self.assertEqual((a["pre_rps"]["min"], a["pre_rps"]["max"]), (10.0, 30.0))
        self.assertEqual(analyze.describe([5.0])["std"], 0.0)


if __name__ == "__main__":
    unittest.main()
