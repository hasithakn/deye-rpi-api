#!/usr/bin/env python3
"""Deye Cloud -> tiny local API.

One poller thread calls Deye /device/latest every POLL_SECONDS and keeps a
single snapshot in memory. HTTP clients only ever read that snapshot, so
client traffic never reaches Deye Cloud.
"""
import hmac
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("deye")


def env(name, default=None):
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"missing required env var: {name}")
    return value


BASE_URL = env("DEYE_BASE_URL", "https://eu1-developer.deyecloud.com").rstrip("/")
APP_ID = env("DEYE_APP_ID")
APP_SECRET = env("DEYE_APP_SECRET")
EMAIL = env("DEYE_EMAIL")
PASSWORD_SHA256 = env("DEYE_PASSWORD_SHA256").lower()
INVERTER_SN = env("DEYE_INVERTER_SN")
API_KEY = env("API_KEY").encode()
POLL_SECONDS = int(env("POLL_SECONDS", "300"))
STALE_SECONDS = int(env("STALE_SECONDS", "900"))
PORT = int(env("PORT", "8080"))
MAX_BACKOFF = 3600

snapshot = None                    # replaced whole by the poller, read by HTTP threads
last_ok = None


class DeyeError(Exception):
    pass


class RateLimited(DeyeError):
    pass


# ---------------------------------------------------------------- Deye client

def post(path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE_URL + path, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited("rate limited (HTTP 429)") from e
        return {"success": False, "code": f"HTTP {e.code}", "msg": e.reason}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise DeyeError(f"{path}: {e}") from e


class Session:
    def __init__(self):
        self.token = None
        self.expires_at = 0.0

    def login(self):
        res = post(f"/v1.0/account/token?appId={APP_ID}",
                   {"appSecret": APP_SECRET, "email": EMAIL, "password": PASSWORD_SHA256})
        if not res.get("accessToken"):
            raise DeyeError(f"login failed: {res.get('code')} {res.get('msg')}")
        expires_in = float(res.get("expiresIn") or 30 * 86400)
        self.token = res["accessToken"]
        self.expires_at = time.time() + max(expires_in - 86400, expires_in / 2)  # renew ~1 day early
        log.info("new token obtained")

    def device_latest(self):
        """One poll. Logs in at most once, retries the data call at most once."""
        logged_in = False
        if not self.token or time.time() > self.expires_at:
            self.login()
            logged_in = True
        body = {"deviceList": [INVERTER_SN]}
        res = post("/v1.0/device/latest", body, self.token)
        if not res.get("success") and not logged_in:
            log.info("device/latest failed (%s %s), logging in again", res.get("code"), res.get("msg"))
            self.login()
            res = post("/v1.0/device/latest", body, self.token)
        if not res.get("success"):
            raise DeyeError(f"device/latest failed: {res.get('code')} {res.get('msg')}")
        return res


def parse(res):
    device = res["deviceDataList"][0]
    values = {d.get("key"): d.get("value") for d in device.get("dataList") or []}

    def num(key):
        try:
            return float(values[key])
        except (KeyError, TypeError, ValueError):
            return None

    soc = num("SOC")
    if soc is None:
        raise DeyeError("SOC missing in device/latest response")
    today = num("PVDailyPowerGenerationActive")
    if today is None:
        today = num("DailyActiveProduction")
    volts, hz = num("GridVoltageL1L2"), num("GridFrequency")
    return {
        "soc": round(soc),
        "today_kwh": None if today is None else round(today, 1),
        "grid": None if volts is None or hz is None else (volts > 150 and hz > 45),
        "collection_time": int(float(device.get("collectionTime") or time.time())),
    }


def poller():
    global snapshot, last_ok
    session, failures = Session(), 0
    while True:
        try:
            snapshot = parse(session.device_latest())
            last_ok, failures, wait = int(time.time()), 0, POLL_SECONDS
            log.info("soc=%s%% today=%skWh grid=%s", snapshot["soc"], snapshot["today_kwh"], snapshot["grid"])
        except RateLimited as e:
            failures, wait = failures + 1, MAX_BACKOFF
            log.warning("%s, waiting %ss", e, wait)
        except Exception as e:  # never let the poller die
            failures = min(failures + 1, 10)
            wait = min(POLL_SECONDS * 2 ** (failures - 1), MAX_BACKOFF)
            log.warning("poll failed: %s, retry in %ss", e, wait)
        time.sleep(wait + random.uniform(0, 30))


# ---------------------------------------------------------------- HTTP API

class Handler(BaseHTTPRequestHandler):
    server_version = "deye-api"
    sys_version = ""

    def send_json(self, code, obj, cache="no-store"):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self.send_json(200, {"ok": snapshot is not None, "last_ok": last_ok})
        if path != "/api/display":
            return self.send_json(404, {"error": "not found"})
        if not hmac.compare_digest(self.headers.get("X-API-Key", "").encode(), API_KEY):
            return self.send_json(401, {"error": "unauthorized"})

        snap = snapshot
        if snap is None:
            return self.send_json(503, {"error": "no data yet"})
        age = max(0, int(time.time()) - snap["collection_time"])
        self.send_json(200, {
            "soc": snap["soc"],
            "today_kwh": snap["today_kwh"],
            "grid": snap["grid"],
            "age_s": age,
            "stale": age > STALE_SECONDS,
        }, cache="private, max-age=60")

    def not_allowed(self):
        self.send_json(405, {"error": "method not allowed"})

    do_POST = do_PUT = do_PATCH = do_DELETE = not_allowed

    def log_message(self, fmt, *args):  # keep logs quiet under heavy traffic
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # fast `docker stop`
    threading.Thread(target=poller, daemon=True).start()
    ThreadingHTTPServer.request_queue_size = 256  # default backlog of 5 drops bursts
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("listening on :%s, polling Deye every %ss", PORT, POLL_SECONDS)
    server.serve_forever()


if __name__ == "__main__":
    main()
