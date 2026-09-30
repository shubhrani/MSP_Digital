"""
temperature_monitor.py
----------------------
Background poller for InfluxDB furnace pyrometer temperatures (PYRO4/1, PYRO4/2).

Runs in its own background thread to decouple InfluxDB queries from the video
capture and YOLO detection loop, ensuring zero camera thread stutter or freeze.
"""

import base64
import csv
import datetime
import io
import logging
import threading
import time
from collections import deque
from typing import Optional, Dict, Any

import requests

import config

logger = logging.getLogger("temperature_monitor")


class TemperatureMonitor:
    """Monitors and caches real-time PYRO temperature from InfluxDB v2."""

    def __init__(
        self,
        group: str,
        field: str,
        url: Optional[str] = None,
        org_id: Optional[str] = None,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        bucket: Optional[str] = None,
        measurement: Optional[str] = None,
        unit: Optional[str] = None,
        poll_interval: Optional[float] = None,
        max_age_seconds: Optional[float] = None,
    ):
        self.group = group
        self.field = field
        self.url = (url or config.INFLUX_URL).rstrip("/")
        self.org_id = org_id or config.INFLUX_ORG_ID or config.INFLUX_ORG
        self.token = token if token is not None else config.INFLUX_TOKEN
        self.username = username if username is not None else config.INFLUX_USERNAME
        self.password = password if password is not None else config.INFLUX_PASSWORD
        self.bucket = bucket or config.INFLUX_BUCKET
        self.measurement = measurement or config.INFLUX_MEASUREMENT
        self.unit = unit or config.INFLUX_UNIT
        self.poll_interval = poll_interval or config.INFLUX_POLL_SECONDS
        self.max_age_seconds = max_age_seconds or config.INFLUX_MAX_AGE_SECONDS

        self._lock = threading.Lock()

        self._latest_value: Optional[float] = None
        self._latest_timestamp: Optional[datetime.datetime] = None
        self._latest_seq: int = 0

        self._previous_value: Optional[float] = None
        self._previous_timestamp: Optional[datetime.datetime] = None
        self._previous_seq: Optional[int] = None

        # Successful HTTP/Influx query time.
        self._last_poll_time: Optional[float] = None

        # Time when a genuinely NEW PYRO sample was received.
        self._last_value_received_monotonic: Optional[float] = None

        # Keep enough data for movement debounce/lookback.
        self._history = deque(maxlen=60)

        self._connected: bool = False
        self._last_error: Optional[str] = None
        self._empty_warned = False
        self._session = requests.Session()
        self._session_authenticated = False

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background temperature polling thread."""
        if not config.INFLUX_ENABLED:
            logger.info("InfluxDB monitoring disabled by config (INFLUX_ENABLED=0).")
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"TempMonitor-{self.group}-{self.field}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Started TemperatureMonitor for group=%s, field=%s (poll=%ss, max_age=%ss)",
            self.group, self.field, self.poll_interval, self.max_age_seconds
        )

    def stop(self):
        """Stop the background polling thread cleanly."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Stopped TemperatureMonitor for %s/%s", self.group, self.field)

    def get_temperature(self) -> Optional[float]:
        """Return latest PYRO value only when the actual sample is fresh."""
        with self._lock:
            if (
                self._latest_value is None
                or self._last_value_received_monotonic is None
            ):
                return None

            data_age = (
                time.monotonic()
                - self._last_value_received_monotonic
            )

            if data_age > self.max_age_seconds:
                return None

            return self._latest_value

    def get_latest(self) -> Dict[str, Any]:
        """Return current PYRO status and sample identity."""
        with self._lock:
            now_wall = time.time()
            now_mono = time.monotonic()

            poll_age = (
                round(now_wall - self._last_poll_time, 2)
                if self._last_poll_time is not None
                else None
            )

            data_age = (
                round(
                    now_mono - self._last_value_received_monotonic,
                    2
                )
                if self._last_value_received_monotonic is not None
                else None
            )

            is_stale = (
                data_age is None
                or data_age > self.max_age_seconds
            )

            return {
                "group": self.group,
                "field": self.field,
                "value": self._latest_value,
                "timestamp": (
                    self._latest_timestamp.isoformat()
                    if self._latest_timestamp else None
                ),
                "seq": self._latest_seq,
                "age_seconds": data_age,
                "data_age_seconds": data_age,
                "poll_age_seconds": poll_age,
                "is_stale": is_stale,
                "connected": self._connected,
                "last_error": self._last_error,
                "url": self.url,
                "bucket": self.bucket,
            }

    def get_recent_samples(self, seconds=None):
        """Return recently received unique PYRO samples."""
        with self._lock:
            samples = list(self._history)

            if seconds is None:
                return [dict(x) for x in samples]

            cutoff = time.monotonic() - float(seconds)

            return [
                dict(x)
                for x in samples
                if x["received_monotonic"] >= cutoff
            ]

    def set_mock_temperature(
        self,
        value: Optional[float],
        timestamp: Optional[datetime.datetime] = None
    ):
        """Inject a new unique PYRO sample for testing."""
        sample_time = timestamp or datetime.datetime.now(
            datetime.timezone.utc
        )

        received_monotonic = time.monotonic()

        with self._lock:
            self._previous_value = self._latest_value
            self._previous_timestamp = self._latest_timestamp
            self._previous_seq = (
                self._latest_seq
                if self._latest_value is not None
                else None
            )

            self._latest_seq += 1

            self._latest_value = value
            self._latest_timestamp = sample_time

            self._last_poll_time = time.time()
            self._last_value_received_monotonic = received_monotonic

            self._connected = True
            self._last_error = None

            self._history.append({
                "seq": self._latest_seq,
                "value": value,
                "source_timestamp": sample_time,
                "received_monotonic": received_monotonic,
            })

    # -----------------------------------------------------------------
    # Polling worker
    # -----------------------------------------------------------------
    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                # Was DEBUG, which hid every 401. WARNING so it shows up.
                logger.warning("Temperature poll failed (%s/%s): %s",
                               self.group, self.field, e)
                with self._lock:
                    self._connected = False
                    if self._last_error is None:
                        self._last_error = str(e)[:500]
            self._stop_event.wait(timeout=self.poll_interval)

    def _ensure_session_authenticated(self):
        """Ensure session cookie is set if using username/password."""
        if self.token or self._session_authenticated:
            return
        if not (self.username and self.password):
            return
        signin_url = f"{self.url}/api/v2/signin"
        try:
            resp = self._session.post(
                signin_url,
                auth=(self.username, self.password),
                timeout=5.0,
            )
            if resp.status_code in (200, 204) or resp.ok:
                self._session_authenticated = True
                logger.info("Successfully authenticated InfluxDB session for user %s", self.username)
            else:
                logger.warning("InfluxDB signin failed (%s): %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("InfluxDB signin exception for user %s: %s", self.username, e)

    def _auth_headers(self):
        """Return the headers that authenticate this monitor's requests.

        Priority:
          1. API token  -> Authorization: Token <token>
          2. Basic auth -> Authorization: Basic <base64(user:pass)>
             and signs in via /api/v2/signin to establish session cookie.
        """
        if self.token:
            return {"Authorization": f"Token {self.token}"}
        if self.username and self.password:
            self._ensure_session_authenticated()
            raw = f"{self.username}:{self.password}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {}

    def _build_flux_query(self) -> str:
        # Look further back than a single poll interval: data may be
        # written in bursts, and a narrow window silently returns nothing.
        # The freshness of the *result* is still enforced by
        # INFLUX_MAX_AGE_SECONDS in get_temperature().
        return f'''
from(bucket: "{self.bucket}")
  |> range(start: -5m)
  |> filter(fn: (r) => r["_measurement"] == "{self.measurement}")
  |> filter(fn: (r) => r["Group"] == "{self.group}")
  |> filter(fn: (r) => r["Unit"] == "{self.unit}")
  |> filter(fn: (r) => r["_field"] == "{self.field}")
  |> last()
'''

    def _poll_once(self):
        if not self.group or not self.field:
            logger.warning("TemperatureMonitor %s/%s has no group/field "
                           "configured, skipping.", self.group, self.field)
            return

        query_url = f"{self.url}/api/v2/query"
        params = {"orgID": self.org_id} if self.org_id else {}
        headers = {
            "Accept": "application/csv",
            "Content-Type": "application/vnd.flux",
        }
        auth = self._auth_headers()
        if not auth:
            logger.warning(
                "TemperatureMonitor %s/%s has no INFLUX_TOKEN and no "
                "INFLUX_USERNAME/INFLUX_PASSWORD set - every query will "
                "return 401.",
                self.group, self.field,
            )
        headers.update(auth)

        flux_query = self._build_flux_query().strip()

        resp = self._session.post(
            query_url,
            params=params,
            data=flux_query,
            headers=headers,
            timeout=6.0,
        )

        if resp.status_code == 401 and self.username and self.password:
            self._session_authenticated = False
            self._ensure_session_authenticated()
            resp = self._session.post(
                query_url,
                params=params,
                data=flux_query,
                headers=headers,
                timeout=6.0,
            )

        if resp.status_code != 200:
            # Full body is logged here, not swallowed - this is exactly
            # how you saw the 401 in the first place.
            logger.warning(
                "Influx query for %s/%s returned HTTP %s: %s",
                self.group, self.field, resp.status_code,
                resp.text[:300].replace("\n", " | "),
            )
            with self._lock:
                self._connected = False
                self._last_error = (
                    f"HTTP {resp.status_code} (auth={'token' if self.token else 'basic' if auth else 'none'}): "
                    f"{resp.text[:200]}"
                )
            resp.raise_for_status()

        val, ts = self._parse_flux_csv(resp.text)

        poll_time = time.time()
        received_monotonic = time.monotonic()

        with self._lock:
            self._connected = True
            self._last_error = None
            self._last_poll_time = poll_time

            if val is None:
                if not self._empty_warned:
                    logger.warning(
                        "Influx query for %s/%s returned no rows (HTTP 200). "
                        "Check Group=%r, Unit=%r, _field=%r, bucket=%r.",
                        self.group, self.field,
                        self.group, self.unit, self.field, self.bucket,
                    )
                    self._empty_warned = True
                return

            self._empty_warned = False

            if ts is None:
                logger.warning("Influx sample has value but no _time for %s/%s",
                               self.group, self.field)
                return

            if (self._latest_timestamp == ts
                    and self._latest_value == val):
                return  # same sample as last poll

            self._previous_value = self._latest_value
            self._previous_timestamp = self._latest_timestamp
            self._previous_seq = (
                self._latest_seq if self._latest_value is not None else None
            )
            self._latest_seq += 1
            self._latest_value = val
            self._latest_timestamp = ts
            self._last_value_received_monotonic = received_monotonic

            self._history.append({
                "seq": self._latest_seq,
                "value": val,
                "source_timestamp": ts,
                "received_monotonic": received_monotonic,
            })

            logger.debug("Influx %s/%s value=%s at %s",
                         self.group, self.field, val, ts.isoformat())

    def _parse_flux_csv(self, csv_text: str):
        """Extracts the float value and timestamp from InfluxDB Flux CSV output."""
        val = None
        ts = None

        f = io.StringIO(csv_text)
        reader = csv.reader(f)
        header = None

        for row in reader:
            if not row:
                continue
            # Header line starts with ,result,table,...
            if len(row) > 1 and row[1] == "result":
                header = row
                continue
            if row[0].startswith("#"):
                continue
            if header and len(row) >= len(header):
                row_dict = dict(zip(header, row))
                raw_val = row_dict.get("_value")
                raw_time = row_dict.get("_time")
                if raw_val is not None:
                    try:
                        val = float(raw_val)
                    except ValueError:
                        pass
                if raw_time:
                    try:
                        # RFC3339 timestamp parsing
                        clean_time = raw_time.replace("Z", "+00:00")
                        ts = datetime.datetime.fromisoformat(clean_time)
                    except Exception:
                        pass

        return val, ts
