"""Offline tests for amcrestctl.py (no camera needed)."""

import contextlib
import http.server
import io
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amcrestctl as a  # noqa: E402

SAMPLE = """\
table.All.NTP.Address=10.6.0.1\r
table.All.NTP.Enable=true\r
table.All.NTP.TimeZone=25\r
table.All.T2UServer.Enable=false\r
table.All.T2UServer.Key=******\r
table.All.T2UServer.UUID=AMC0000000000000\r
table.All.NAS[0].Address=0.0.0.0\r
table.All.NAS[0].Password=secret\r
table.All.NAS[1].Enable=false\r
table.All.General.MachineName=AMC0000000000000\r
table.All.Locales.DSTStart.Month=3\r
table.All.Locales.DSTStart.Year=2005\r
"""


class Parse(unittest.TestCase):
    def test_table(self):
        t = a.parse_table(SAMPLE)
        self.assertEqual(t["NTP.Address"], "10.6.0.1")
        self.assertEqual(t["NAS[0].Address"], "0.0.0.0")

    def test_table_single_name(self):
        self.assertEqual(a.parse_table("table.NTP.Address=10.6.0.1\n"), {"NTP.Address": "10.6.0.1"})

    def test_error_answer(self):
        with self.assertRaises(RuntimeError):
            a.parse_table("Error\r\nBad Request!\r\n")

    def test_managed_keeps_only_listed_keys(self):
        m = a.managed(a.parse_table(SAMPLE))
        flat = {k for g in m.values() for k in g}
        self.assertEqual(flat, {"NTP.Address", "NTP.Enable", "NTP.TimeZone", "T2UServer.Enable",
                                "NAS[0].Address", "NAS[1].Enable", "Locales.DSTStart.Month"})
        self.assertEqual(m["outbound"]["NAS[0].Address"], "0.0.0.0")
        self.assertEqual(m["cloud"], {"T2UServer.Enable": "false"})

    def test_no_secret_or_serial_is_managed(self):
        for k in ("T2UServer.Key", "T2UServer.Username", "T2UServer.UUID", "NAS[0].Password",
                  "General.MachineName", "Bonjour.Name", "Email.Password", "DDNS[0].Password",
                  "Network.eth0.PhysicalAddress", "DeviceInfo.Serial"):
            self.assertIsNone(a.group_of(k), k)

    def test_index_pattern(self):
        self.assertEqual(a.group_of("NAS[12].Enable"), "outbound")
        self.assertIsNone(a.group_of("NAS.Enable"))


class Yaml(unittest.TestCase):
    def test_roundtrip(self):
        d = {"schema": "1", "host": "10.6.128.14", "vault_path": "homelab/cameras/rear",
             "device": {"type": "IP5M"},
             "settings": a.managed(a.parse_table(SAMPLE))}
        text = a.dump_yaml(d)
        self.assertEqual(a.load_yaml(text), d)
        self.assertIn('"NAS[0].Address": "0.0.0.0"', text)
        self.assertLess(text.index("time:"), text.index("cloud:"))

    def test_committed_files_check(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = a.main(["check", "--all"])
        self.assertEqual(rc, 0, out.getvalue())


class Compare(unittest.TestCase):
    def setUp(self):
        self.desired = {"settings": {"time": {"NTP.Address": "10.6.0.1", "NTP.Enable": "true"},
                                     "cloud": {"T2UServer.Enable": "false"}}}

    def test_no_drift(self):
        live = {"NTP.Address": "10.6.0.1", "NTP.Enable": "true", "T2UServer.Enable": "false",
                "T2UServer.Key": "x"}
        self.assertEqual(a.compare(self.desired, live), ([], [], []))

    def test_changed_new_unread(self):
        live = {"NTP.Address": "pool.ntp.org", "T2UServer.Enable": "false", "UPnP.Enable": "true"}
        changed, new, unread = a.compare(self.desired, live)
        self.assertEqual(changed, [("NTP.Address", "10.6.0.1", "pool.ntp.org")])
        self.assertEqual(new, [("UPnP.Enable", "true")])
        self.assertEqual(unread, [("NTP.Enable", "true")])

    def test_set_request(self):
        self.assertEqual(a.set_request("NAS[0].Address", "0.0.0.0"),
                         "/cgi-bin/configManager.cgi?action=setConfig&NAS[0].Address=0.0.0.0")
        self.assertIn("NTP.TimeZoneDesc=Eastern+Time", a.set_request("NTP.TimeZoneDesc", "Eastern Time"))


class Digest(unittest.TestCase):
    def test_rfc2617_vector(self):
        chal = ('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
        hdr = a._digest_header(chal, "Mufasa", "Circle Of Life", "GET", "/dir/index.html", 1)
        # cnonce is random, so recompute the expected response from the header's cnonce
        import hashlib
        import re
        cn = re.search(r'cnonce="([^"]+)"', hdr).group(1)
        h = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
        want = h("%s:dcd98b7102dd2f0e8b11d0f600bfb0c093:00000001:%s:auth:%s" % (
            h("Mufasa:testrealm@host.com:Circle Of Life"), cn, h("GET:/dir/index.html")))
        self.assertIn('response="%s"' % want, hdr)
        self.assertIn('opaque="5ccc069c403ebaf9f0171e9517f40e41"', hdr)
        self.assertIn("nc=00000001", hdr)


class _Handler(http.server.BaseHTTPRequestHandler):
    calls = []
    accept = True

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization")
        type(self).calls.append(bool(auth))
        if auth and type(self).accept:
            body = SAMPLE.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Digest realm="Login to AMC", qop="auth", nonce="abc", opaque="x"')
        self.send_header("Content-Length", "0")
        self.end_headers()


class OneAttempt(unittest.TestCase):
    def setUp(self):
        _Handler.calls = []
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cam = a.Camera("127.0.0.1:%d" % self.srv.server_port, "admin", "pw", timeout=5)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_reads_config(self):
        _Handler.accept = True
        self.assertEqual(self.cam.config()["NTP.Address"], "10.6.0.1")
        self.assertEqual(_Handler.calls, [False, True])

    def test_rejected_password_is_not_retried(self):
        _Handler.accept = False
        with self.assertRaisesRegex(RuntimeError, "not retrying"):
            self.cam.config()
        self.assertEqual(_Handler.calls, [False, True])


if __name__ == "__main__":
    unittest.main()
