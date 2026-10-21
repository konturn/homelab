"""anomaly-detector CLI.

  python -m detector serve                   # live loop + HTTP API + MQTT
  python -m detector once                    # one forced cycle, print anomalies
  python -m detector backtest --days 21      # what would have been flagged
  python -m detector test-anomaly            # one labelled synthetic severe anomaly
  python -m detector validate                # parse config + suppressions
"""
import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import threading
import time

from . import backtest, mqtt, server
from .config import load_config, load_suppressions, parse_suppressions
from .engine import Engine
from .influx import Influx, line

log = logging.getLogger("detector")


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"missing required environment variable {name}")
    return v


def make_influx(write=False):
    return Influx(env("INFLUX_URL", "http://influxdb:8086"), env("INFLUX_ORG", "homelab"),
                  env("INFLUX_READ_TOKEN", required=True),
                  env("INFLUX_WRITE_TOKEN", required=write))


def cmd_serve(args):
    cfg = load_config(args.config)
    engine = Engine(cfg, make_influx(write=True), args.suppressions, env("INFLUX_WRITE_BUCKET", "metrics"))
    lock = threading.Lock()
    pub = None
    if env("MQTT_HOST"):
        pub = mqtt.Publisher(env("MQTT_HOST"), int(env("MQTT_PORT", "8883")),
                             env("MQTT_USERNAME", required=True), env("MQTT_PASSWORD", required=True),
                             tls=env("MQTT_TLS", "true") == "true")
        pub.set_discovery(mqtt.discovery_messages([s.name for s in cfg.signals]))

    def extra():
        return {"mqtt_connected": bool(pub and pub.connected) if pub else None,
                "last_cycle_at": engine.last_cycle_at}

    nets = server.parse_networks(env("ALLOWED_CLIENTS", ""))
    srv = server.serve(engine, lock, "0.0.0.0", int(env("PORT", "8080")), nets, extra)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("serving: %d signals, cycle %ss", len(cfg.signals), cfg.cycle_seconds)
    while not stop.is_set():
        t0 = time.time()
        with lock:
            engine.run_cycle()
            summary = engine.signal_summary()
        if pub:
            pub.publish(mqtt.state_messages(summary))
        c = engine.counts()
        log.info("cycle %.1fs severe=%d moderate=%d suppressed=%d errors=%d queries=%d",
                 engine.last_cycle_seconds, c["severe"], c["moderate"], c["suppressed"],
                 len(engine.errors), engine.influx.queries)
        stop.wait(max(5, cfg.cycle_seconds - (time.time() - t0)))
    if pub:
        pub.close()
    srv.shutdown()


def cmd_once(args):
    cfg = load_config(args.config)
    engine = Engine(cfg, make_influx(), args.suppressions)
    engine.influx.write = lambda *a, **k: None  # dry run: never write
    engine.run_cycle(force=True)
    st = engine.status()
    out = {k: st[k] for k in ("counts", "anomalies", "suppressed", "pending", "errors",
                               "insufficient_history")}
    out["queries"], out["seconds"] = engine.influx.queries, round(engine.last_cycle_seconds, 2)
    print(json.dumps(out, indent=1, default=str))


def cmd_backtest(args):
    cfg = load_config(args.config)
    now = time.time()
    today = dt.date.today()
    sups = load_suppressions(args.suppressions, today) if args.suppressions else []
    influx = make_influx()
    rep = backtest.run(cfg, influx, args.days, now, sups, set(args.signal) if args.signal else None)
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        print(backtest.format_report(rep, args.days))
        print(f"({influx.queries} queries, {influx.query_seconds:.1f}s)")


def cmd_test_anomaly(args):
    """Write one clearly labelled synthetic severe anomaly, for end-to-end tests.

    signal=test_synthetic, category=test. The Grafana rule passes the labels
    through, and the OpenClaw triage instructions prefix the message [TEST].
    """
    influx = make_influx(write=True)
    bucket = env("INFLUX_WRITE_BUCKET", "metrics")
    end = time.time() + args.minutes * 60
    n = 0
    while True:
        influx.write(bucket, [line("anomaly", {
            "signal": "test_synthetic", "check": "threshold", "series": "test",
            "severity": "severe", "suppressed": "false", "category": "test"},
            {"score": 99.0, "value": 1.0, "baseline": 0.0, "anomalous": 1}, time.time())])
        n += 1
        if time.time() + 60 > end:
            break
        time.sleep(60)
    print(f"wrote {n} synthetic anomaly point(s) for signal=test_synthetic")


def cmd_validate(args):
    cfg = load_config(args.config)
    import yaml
    with open(args.suppressions) as f:
        sups = parse_suppressions(yaml.safe_load(f), dt.date.today(), strict=True)
    print(f"ok: {len(cfg.signals)} signals, "
          f"{sum(len(s.checks) for s in cfg.signals)} checks, {len(sups)} suppressions")


def main(argv=None):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="detector")
    ap.add_argument("--config", default=env("CONFIG", "/config/config.yml"))
    ap.add_argument("--suppressions", default=env("SUPPRESSIONS", "/config/suppressions.yml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("once")
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=int, default=21)
    b.add_argument("--signal", action="append")
    b.add_argument("--json", action="store_true")
    t = sub.add_parser("test-anomaly")
    t.add_argument("--minutes", type=int, default=8)
    sub.add_parser("validate")
    args = ap.parse_args(argv)
    {"serve": cmd_serve, "once": cmd_once, "backtest": cmd_backtest,
     "test-anomaly": cmd_test_anomaly, "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    main()
