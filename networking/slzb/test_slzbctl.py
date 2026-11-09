"""Offline tests for slzbctl.py (no coordinator needed)."""

import contextlib
import http.server
import io
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slzbctl as s  # noqa: E402

HDR = {"DEVICE": "SLZB-MR1", "DEVICE_NAME": "SLZB-MR1", "BUILD_DATE": 20260425, "current-time": "05:26",
       "uptime": "[U_time]^  2^22^40", "zbStatus": "[S_Y_con]^1", "zbConnT": "x", "zbConn2": "x",
       "lng_0": "", "H_modeUsb": "", "H_serial2": ""}
PAGES = {
    0: {**HDR, "ZbAutoUpd": "[OFF]", "ethMac": "00:00:00:00:00:01", "esp32Temp": "62.78", "ipv6": "[OFF]"},
    1: {**HDR, "port": 6638, "wbrEn": False, "passWBR": "secret", "mrSerialMutex": False},
    4: {**HDR, "login": "admin", "pass": "admin", "fwIp": "", "apPass": "x", "apPassEnabled": False},
    9: {**HDR, "EET-2EEST,M3.5.0/3,M10.5.0/4": True, "server1": "pool.ntp.org", "use12h": False,
        "curDatetime": "Monday"},
    15: {**HDR, "zbCh": 18, "zbPanId": 1234, "zbExtPanId": "1,2,3"},
}
INFO = {"Info": {"model": "SLZB-MR1", "sw_version": "v3.3.1", "fw_channel": "release", "MAC": "00:00:00:00:00:02",
                 "radios": [{"chip_index": 0, "zb_hw": "EFR32MG21", "zb_version": 20250220}]}}
SENSORS = {"Sensors": {"esp32_temp": "62.78", "uptime": 1, "disable_leds": False, "night_mode": True}}


class Filter(unittest.TestCase):
    def test_secrets_and_status_dropped(self):
        self.assertEqual(s.page_settings(4, PAGES[4]), {"fwIp": ""})
        self.assertEqual(s.page_settings(1, PAGES[1]), {"port": "6638", "wbrEn": "false", "mrSerialMutex": "false"})
        self.assertEqual(s.page_settings(15, PAGES[15]), {"zbCh": "18"})

    def test_page0_whitelist(self):
        self.assertEqual(s.page_settings(0, PAGES[0]), {"ZbAutoUpd": "[OFF]", "ipv6": "[OFF]"})

    def test_timezone_normalised(self):
        self.assertEqual(s.page_settings(9, PAGES[9]),
                         {"server1": "pool.ntp.org", "use12h": "false", "timezone": "EET-2EEST,M3.5.0/3,M10.5.0/4"})


class Yaml(unittest.TestCase):
    def test_roundtrip(self):
        d = {"schema": "1", "host": "10.6.128.20", "device": {"firmware": "v3.3.1"},
             "settings": {"serial": {"115200": "true"}, "time": {"timezone": "EST5EDT,M3.2.0,M11.1.0"}}}
        self.assertEqual(s.load_yaml(s.dump_yaml(d)), d)

    def test_committed_file_checks(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = s.main(["check"])
        self.assertEqual(rc, 0, out.getvalue())

    def test_check_rejects_secret(self):
        self.assertTrue(s.is_secret_key("passW"))
        self.assertTrue(s.is_secret_key("ethMac"))
        self.assertFalse(s.is_secret_key("mrSerialMutex"))


class Compare(unittest.TestCase):
    def test_changed_new_unread(self):
        want = {"device": {"firmware": "v3.3.1"}, "settings": {"time": {"server1": "10.6.0.1", "use12h": "false"}}}
        have = {"device": {"firmware": "v3.3.2"}, "settings": {"time": {"server1": "10.6.0.1"},
                                                             "syslog": {"syslogEn": "true"}}}
        changed, new, unread = s.compare(want, have)
        self.assertEqual(changed, [(("device", "firmware"), "v3.3.1", "v3.3.2")])
        self.assertEqual(new, [(("settings", "syslog", "syslogEn"), "true")])
        self.assertEqual(unread, [(("settings", "time", "use12h"), "false")])


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        type(self).seen.append(("GET", self.path))
        if self.path == "/ha_info":
            return self._send(200, json.dumps(INFO).encode())
        if self.path == "/ha_sensors":
            return self._send(200, json.dumps(SENSORS).encode())
        if self.path.startswith("/api2?action=0&page="):
            n = int(self.path.rsplit("=", 1)[1])
            if n in PAGES:
                return self._send(200, b"<gz>", {"respValuesArr": json.dumps(PAGES[n])})
            if n <= 18:
                return self._send(200, b"<gz>", {"respValuesArr": json.dumps(HDR)})
            return self._send(500)
        return self._send(404)

    def do_POST(self):  # noqa: N802
        type(self).seen.append(("POST", self.path))
        self._send(405)


class Read(unittest.TestCase):
    def test_read_only_gets(self):
        _Handler.seen = []
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            live = s.Coordinator("127.0.0.1:%d" % srv.server_port, timeout=5).read()
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(live["device"], {"model": "SLZB-MR1", "firmware": "v3.3.1", "fw_channel": "release",
                                          "radio0": "EFR32MG21 20250220"})
        self.assertEqual(live["settings"]["mode"]["port"], "6638")
        self.assertEqual(live["settings"]["security"], {"fwIp": ""})
        self.assertEqual(live["settings"]["sensors"], {"disable_leds": "false"})
        self.assertNotIn("page_11", live["settings"])  # only the common header: no settings
        self.assertTrue(all(m == "GET" for m, _ in _Handler.seen))
        self.assertTrue(all(p.startswith(("/api2?action=0&", "/ha_info", "/ha_sensors")) for _, p in _Handler.seen))


if __name__ == "__main__":
    unittest.main()
