"""check_influx.py - diagnose the InfluxDB connection end to end.

Run:
    python3 check_influx.py
"""
import socket
import sys

import requests

import config


def main():
    print("=" * 70)
    print("INFLUXDB CONNECTION TEST")
    print("=" * 70)
    print(f"URL         : {config.INFLUX_URL}")
    print(f"Org ID      : {config.INFLUX_ORG_ID!r}")
    print(f"Bucket      : {config.INFLUX_BUCKET!r}")
    print(f"Measurement : {config.INFLUX_MEASUREMENT!r}")
    print(f"Unit        : {config.INFLUX_UNIT!r}")
    print(f"Token set   : {'YES (len=%d)' % len(config.INFLUX_TOKEN) if config.INFLUX_TOKEN else 'NO (empty)'}")
    print(f"User set    : {'YES' if config.INFLUX_USERNAME else 'NO (empty)'}")
    print(f"Pass set    : {'YES' if config.INFLUX_PASSWORD else 'NO (empty)'}")
    print("=" * 70)

    host = config.INFLUX_URL.split("//", 1)[-1].split(":")[0].split("/")[0]

    # --- 1. DNS + TCP reachability ------------------------------------
    print("\n[1] Network reachability")
    try:
        ip = socket.gethostbyname(host)
        print(f"    OK   DNS: {host} -> {ip}")
    except Exception as exc:
        print(f"    FAIL DNS: {exc}")

    for port in (8086, 80, 443):
        try:
            s = socket.create_connection((host, port), timeout=3)
            s.close()
            print(f"    OK   TCP {host}:{port} reachable")
        except Exception as exc:
            print(f"    ---- TCP {host}:{port} not reachable ({exc})")

    # --- 2. InfluxDB /health ------------------------------------------
    print("\n[2] InfluxDB /health")
    try:
        r = requests.get(f"{config.INFLUX_URL}/health", timeout=5)
        print(f"    HTTP {r.status_code}: {r.text[:300]}")
    except Exception as exc:
        print(f"    FAIL: {exc}")

    # --- 3. Run the same query the app runs, per furnace --------------
    import base64 as _b64

    session_cookies = {}
    if config.INFLUX_TOKEN:
        auth_header = {"Authorization": f"Token {config.INFLUX_TOKEN}"}
        print("\nAuth: token")
    elif config.INFLUX_USERNAME and config.INFLUX_PASSWORD:
        # InfluxDB 2.x OSS requires /api/v2/signin to establish session cookie
        try:
            r_sign = requests.post(
                f"{config.INFLUX_URL}/api/v2/signin",
                auth=(config.INFLUX_USERNAME, config.INFLUX_PASSWORD),
                timeout=5,
            )
            if r_sign.status_code in (200, 204) and "influxdb-oss-session" in r_sign.cookies:
                session_cookies = {"influxdb-oss-session": r_sign.cookies["influxdb-oss-session"]}
                print(f"\nAuth: basic session signin OK ({config.INFLUX_USERNAME})")
            else:
                print(f"\nAuth: basic signin returned HTTP {r_sign.status_code}: {r_sign.text[:200]}")
        except Exception as exc:
            print(f"\nAuth: basic signin error: {exc}")

        raw = f"{config.INFLUX_USERNAME}:{config.INFLUX_PASSWORD}".encode()
        auth_header = {"Authorization": "Basic " + _b64.b64encode(raw).decode()}
    else:
        auth_header = {}
        print("\nAuth: NONE - this is why you will get a 401")

    slots = getattr(config, "FURNACE_SLOTS", None) or getattr(config, "FURNACES", {})
    for slug, cfg in slots.items():
        group = cfg.get("influx_group")
        field = cfg.get("pyro_field")
        print(f"\n[3] Query furnace slot {slug}  (Group={group!r}, _field={field!r})")

        flux = f'''
from(bucket: "{config.INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r["_measurement"] == "{config.INFLUX_MEASUREMENT}")
  |> filter(fn: (r) => r["Group"] == "{group}")
  |> filter(fn: (r) => r["Unit"] == "{config.INFLUX_UNIT}")
  |> filter(fn: (r) => r["_field"] == "{field}")
  |> last()
'''.strip()

        headers = {
            "Accept": "application/csv",
            "Content-Type": "application/vnd.flux",
            **auth_header,
        }

        params = {}
        if config.INFLUX_ORG_ID:
            params["orgID"] = config.INFLUX_ORG_ID

        try:
            r = requests.post(
                f"{config.INFLUX_URL}/api/v2/query",
                params=params, data=flux, headers=headers,
                cookies=session_cookies, timeout=8,
            )
            print(f"    HTTP {r.status_code}")
            body = r.text[:800]
            print("    body:")
            for line in body.splitlines():
                print("      " + line)
            if r.status_code == 200 and "_value" not in r.text:
                print("    >> Query succeeded but returned no rows.")
                print("       Check Group / Unit / _field values.")
        except Exception as exc:
            print(f"    FAIL: {exc}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main() or 0)
