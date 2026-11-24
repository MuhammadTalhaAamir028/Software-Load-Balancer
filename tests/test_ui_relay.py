"""Tests for the pure parts of ui/relay.py (no Docker needed).

Run from the repository root:  python -m unittest tests.test_ui_relay -v
"""
import datetime
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ui"))

import relay  # noqa: E402


def packet(stream, t, src, dst, sport, dport, flags, method="", uri="", code="", ua="", xff="", conn=""):
    return {"frame.number": "1", "frame.time_relative": str(t), "ip.src": src, "ip.dst": dst,
            "tcp.srcport": str(sport), "tcp.dstport": str(dport), "tcp.flags": flags, "tcp.stream": str(stream),
            "http.request.method": method, "http.request.uri": uri, "http.response.code": code,
            "http.user_agent": ua, "http.x_forwarded_for": xff, "http.connection": conn}


class ParserTests(unittest.TestCase):
    def test_netem_state_is_one_line(self):
        self.assertEqual(relay.parse_netem_state("RTT_MS=100 LOSS_PCT=2.5"), {"RTT_MS": 100.0, "LOSS_PCT": 2.5})
        self.assertEqual(relay.parse_netem_state(""), {"RTT_MS": 0.0, "LOSS_PCT": 0.0})
        self.assertEqual(relay.parse_netem_state("RTT_MS=abc"), {"RTT_MS": 0.0, "LOSS_PCT": 0.0})

    def test_ping_summary(self):
        text = "3 packets transmitted, 3 received, 0% packet loss, time 405ms\nrtt min/avg/max/mdev = 100.2/100.4/100.5/0.1 ms"
        self.assertEqual(relay.parse_ping(text), {"rtt_avg_ms": 100.4, "loss_pct": 0.0})
        self.assertEqual(relay.parse_ping("nothing"), {"rtt_avg_ms": None, "loss_pct": None})

    def test_cpu_limit_uses_quota_when_nanocpus_is_zero(self):
        self.assertEqual(relay.cpu_limit(0, 50000, 100000), 0.5)
        self.assertEqual(relay.cpu_limit(0, 25000, 100000), 0.25)
        self.assertEqual(relay.cpu_limit(1_500_000_000, 0, 0), 1.5)
        self.assertEqual(relay.cpu_limit(0, 0, 0), "unlimited")

    def test_iso_to_ms_handles_nanoseconds(self):
        expected = int(datetime.datetime(2026, 11, 21, 16, 10, 51, tzinfo=datetime.timezone.utc).timestamp()) * 1000 + 354
        self.assertEqual(relay.iso_to_ms("2026-11-21T16:10:51.354Z"), expected)
        self.assertEqual(relay.iso_to_ms("2026-11-21T16:10:51.354123456Z"), expected)
        self.assertIsNone(relay.iso_to_ms("not a time"))

    def test_tail_text_drops_the_cut_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.jsonl")
            with open(path, "w", newline="\n") as fh:
                fh.write("".join(f"line number {i}\n" for i in range(1000)))
            import pathlib
            text = relay.tail_text(pathlib.Path(path), max_bytes=200)
            self.assertLessEqual(len(text), 200)
            self.assertTrue(all(line.startswith("line number") for line in text.splitlines()))
            self.assertTrue(text.endswith("line number 999\n"))

    def test_haproxy_csv_rows(self):
        text = "# pxname,svname,status,stot,rtime,\nbe,b3,UP,42,7,\nbe,BACKEND,UP,100,5,\n"
        rows = relay.parse_haproxy_csv(text)
        self.assertEqual([r["svname"] for r in rows], ["b3", "BACKEND"])
        self.assertEqual(rows[0]["stot"], "42")
        self.assertEqual(relay.parse_haproxy_csv(""), [])

    def test_haproxy_events_are_deduplicated(self):
        log = ("2026-11-21T16:10:51.354Z [WARNING]  (1) : Server be/b3 is DOWN, reason: Layer4 connection problem, info: x\n"
               "2026-11-21T16:10:51.354Z Server be/b3 is DOWN, reason: Layer4 connection problem, info: x\n")
        events = relay.parse_haproxy_events(log)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["backend"], events[0]["state"], events[0]["reason"]),
                         ("b3", "DOWN", "Layer4 connection problem"))

    def test_own_events(self):
        log = ('{"ts":"2026-11-21T16:10:51.354Z","event":"request","status":200}\n'
               '{"ts":"2026-11-21T16:10:51.354Z","event":"backend_down","backend":"b3","reason":"3 consecutive failures: refused"}\n')
        events = relay.parse_own_events(log)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["backend"], events[0]["state"]), ("b3", "DOWN"))


class HaproxySummaryTests(unittest.TestCase):
    def test_backend_row_gives_the_totals(self):
        rows = [{"pxname": "be", "svname": "b1", "stot": "5"},
                {"pxname": "be", "svname": "BACKEND", "stot": "120", "scur": "7", "rtime": "13", "econ": "2", "eresp": "1"}]
        self.assertEqual(relay.haproxy_summary(rows), {"total": 120, "scur": 7, "rtime_ms": 13.0, "errors": 3})

    def test_missing_backend_row_or_empty_fields_give_zeros(self):
        zero = {"total": 0, "scur": 0, "rtime_ms": 0.0, "errors": 0}
        self.assertEqual(relay.haproxy_summary([]), zero)
        self.assertEqual(relay.haproxy_summary([{"pxname": "be", "svname": "BACKEND", "stot": "", "rtime": "x"}]), zero)

    def test_both_is_an_allowed_active_system(self):
        self.assertEqual(relay.ACTIVE_SYSTEMS, {"own", "haproxy", "both"})


class LogTailTests(unittest.TestCase):
    def test_reads_only_new_lines_and_waits_for_complete_lines(self):
        import pathlib
        down = '{"ts":"2026-11-21T16:10:51.354Z","event":"backend_down","backend":"b3","reason":"refused"}\n'
        up = '{"ts":"2026-11-21T16:11:10.000Z","event":"backend_up","backend":"b3","reason":"recovered"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "lb.jsonl"
            path.write_bytes(b"")
            tail = relay.LogTail(path)
            self.assertEqual(tail.read(), [])
            with open(path, "ab") as fh:
                fh.write(b'{"ts":"2026-11-21T16:10:50.000Z","event":"request","status":200}\n' * 1000)
                fh.write(down.encode())
                fh.write(up.encode()[:30])                       # a half-written line
            self.assertEqual([e["state"] for e in tail.read()], ["DOWN"])
            with open(path, "ab") as fh:
                fh.write(up.encode()[30:])                       # the rest arrives later
                fh.write(b'{"ts":"2026-11-21T16:11:11.000Z","event":"request","status":200}\n' * 5000)
            self.assertEqual([e["state"] for e in tail.read()], ["DOWN", "UP"])   # the DOWN is still remembered
            self.assertEqual([e["state"] for e in tail.read()], ["DOWN", "UP"])


class GuardTests(unittest.TestCase):
    def test_host_must_be_local(self):
        self.assertTrue(relay.check_request("GET", {"Host": "127.0.0.1:8765"})[0])
        self.assertTrue(relay.check_request("GET", {"Host": "localhost:8765"})[0])
        self.assertFalse(relay.check_request("GET", {"Host": "evil.example"})[0])

    def test_post_needs_header_and_same_origin(self):
        good = {"Host": "127.0.0.1:8765", "X-UI-Control": "1", "Origin": "http://127.0.0.1:8765"}
        self.assertTrue(relay.check_request("POST", good)[0])
        self.assertTrue(relay.check_request("POST", {"Host": "127.0.0.1:8765", "X-UI-Control": "1"})[0])
        self.assertFalse(relay.check_request("POST", {**good, "Origin": "http://evil.example"})[0])
        self.assertFalse(relay.check_request("POST", {"Host": "127.0.0.1:8765"})[0])


class AnnotateTests(unittest.TestCase):
    def rows(self):
        ua = relay.DEMO_UA
        c, lb = "10.0.1.11", "10.0.2.10"
        rows = [packet(0, 0.0, c, lb, 40000, 8080, "0x0002"),
                packet(0, 0.1, lb, c, 8080, 40000, "0x0012"),
                packet(0, 0.2, c, lb, 40000, 8080, "0x0010")]
        for i in range(3):
            rows.append(packet(0, 0.3 + i, c, lb, 40000, 8080, "0x0018", method="GET", uri="/", ua=ua))
            rows.append(packet(0, 0.4 + i, lb, c, 8080, 40000, "0x0018", code="200"))
        rows.append(packet(0, 5.0, c, lb, 40000, 8080, "0x0011"))
        # forwarded leg: LB to b2, own connection, X-Forwarded-For and Connection: close
        rows += [packet(1, 0.31, lb, "10.0.3.12", 50000, 8000, "0x0002"),
                 packet(1, 0.32, lb, "10.0.3.12", 50000, 8000, "0x0018", method="GET", uri="/", ua=ua, xff=c, conn="close")]
        # two health probes of b3, 1 s apart
        rows += [packet(2, 1.0, lb, "10.0.3.13", 50100, 8000, "0x0002"),
                 packet(2, 1.01, lb, "10.0.3.13", 50100, 8000, "0x0018", method="GET", uri="/health", ua="-"),
                 packet(3, 2.0, lb, "10.0.3.13", 50101, 8000, "0x0002"),
                 packet(3, 2.01, lb, "10.0.3.13", 50101, 8000, "0x0018", method="GET", uri="/health", ua="-")]
        # a refused connection, and a stream of background load
        rows += [packet(4, 3.0, lb, "10.0.3.14", 50200, 8000, "0x0002"),
                 packet(4, 3.0, "10.0.3.14", lb, 8000, 50200, "0x0014")]
        rows.append(packet(5, 0.5, "10.0.1.12", lb, 41000, 8080, "0x0018", method="GET", uri="/", ua="hey/0.0.1"))
        return rows

    def test_summary_counts_and_notes(self):
        result = relay.annotate_packets(self.rows())
        summary = result["summary"]
        self.assertEqual(summary["demo_client"], {"connections": 1, "syn": 1, "requests": 3, "responses": 3})
        self.assertEqual(summary["demo_forward"], [{"backend": "10.0.3.12", "x_forwarded_for": "10.0.1.11", "connection": "close"}])
        self.assertEqual(summary["probes"]["count"], 2)
        self.assertEqual(summary["probes"]["backends"]["10.0.3.13"]["interval_s"], 1.0)
        self.assertEqual(summary["refused"], {"count": 1, "backends": {"10.0.3.14": 1}})
        self.assertEqual(summary["other_streams"], 1)
        self.assertTrue(any("3 HTTP requests used 1 TCP connection (1 SYN)" in note for note in result["notes"]))

    def test_events_label_the_handshake_requests_and_close(self):
        events = relay.annotate_packets(self.rows())["events"]
        client = [e["label"] for e in events if e["leg"] == "client-LB"]
        self.assertEqual(client[:3], ["SYN", "SYN-ACK", "ACK"])
        self.assertEqual(client.count("HTTP GET /"), 3)
        self.assertIn("FIN", client)
        forward = [e for e in events if e["leg"] == "LB-backend"]
        self.assertIn("X-Forwarded-For: 10.0.1.11", forward[-1]["detail"])

    def test_missing_demo_is_reported(self):
        notes = relay.annotate_packets([])["notes"]
        self.assertIn("not found", notes[0])


if __name__ == "__main__":
    unittest.main()
