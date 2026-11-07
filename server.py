#!/usr/bin/env python3
"""Claude usage dashboard.

Pulls token usage and cost for your Anthropic organization from the Usage and
Cost Analytics API (per API key, model, product, workspace), supplements
"today" with the fresher legacy usage report, and optionally parses local
Claude Code transcripts for subscription-billed usage that no API reports.

Serves the dashboard at http://localhost:PORT and refreshes in the background.
Standard library only.

Config: `.env` (ANTHROPIC_ADMIN_KEY) and `config.json`, read from
  ~/Library/Application Support/claude-usage-dashboard/ first, then from this folder.
State: `cache/` next to this file (safe to delete; it is rebuilt on start).
Logs:  ~/Library/Logs/claude-usage-dashboard.log when run under launchd (install.sh);
  stdout/stderr when run in the foreground. Every line carries a timestamp.
Test:  `python3 server.py --test` scans the local sources once and exits without
  opening a port, so it can run beside the installed instance.
"""
import datetime as dt
import glob
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
# Config is looked up here first (the agent-coding convention), then in ROOT.
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/claude-usage-dashboard")
API_BASE = "https://api.anthropic.com"
ANALYTICS_BETA = "analytics-usage-cost-2026-08-24"
ANALYTICS_EARLIEST = dt.date(2026, 1, 1)
USER_AGENT = "claude-usage-dashboard/1.0 (local)"

DEFAULT_CONFIG = {
    "port": 8787,
    "history_days": 90,          # how far back to backfill the Analytics API
    "revisit_days": 10,          # re-fetch this many recent days each refresh (data gets revised)
    "analytics_refresh_min": 30, # Analytics API data refreshes ~every 4h; polling more often is harmless
    "live_refresh_min": 5,       # legacy usage report: fresh within ~5 min, poll <= 1/min allowed
    "local_refresh_sec": 60,     # local Claude Code transcripts
    "claude_projects_dir": "~/.claude/projects",
    "app_usage_dir": "~/.claude/app-usage",   # per-call usage logs written by apps (see README)
    "timezone": "America/Los_Angeles",  # IANA name; days are bucketed in this zone
    "day_start_hour": 4,         # a "day" runs from 04:00 to 04:00 local time
    "app_names": {},             # display overrides keyed by API key id, API key name, app-log app
                                 # name, or the Agent SDK working directory (full path or suffix)
}

# USD per million tokens: (input, output, cache_read, cache_write_5m, cache_write_1h).
# Sources: Anthropic pricing docs as of 2026-10. Longest prefix wins.
PRICING = {
    "claude-fable-5-1":  (10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-mythos-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-fable-5":    (10.0, 50.0, 1.00, 12.5, 20.0),
    "claude-mythos-5":   (10.0, 50.0, 1.00, 12.5, 20.0),
    "claude-opus-5-5":   (4.0, 20.0, 0.20, 5.0, 8.0),
    "claude-opus-5":     (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-8":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-7":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-6":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-5":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-1":   (15.0, 75.0, 1.50, 18.75, 30.0),
    "claude-opus-4":     (15.0, 75.0, 1.50, 18.75, 30.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5, 4.0),
    "claude-sonnet-5":   (2.0, 10.0, 0.20, 2.5, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-sonnet-4":   (3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-sonnet-3-7": (3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-haiku-5-5":  (0.10, 0.50, 0.01, 0.125, 0.20),
    "claude-haiku-4-5":  (1.0, 5.0, 0.10, 1.25, 2.0),
    "claude-haiku-3-5":  (0.80, 4.0, 0.08, 1.0, 1.6),
    "claude-3-5-haiku":  (0.80, 4.0, 0.08, 1.0, 1.6),
}


def price_for(model):
    if not model:
        return None
    best = None
    for prefix, rates in PRICING.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICING.get(best)


def estimate_cost(model, uncached, cache_read, cache_5m, cache_1h, output):
    """Returns USD or None when the model is unpriced."""
    r = price_for(model)
    if not r:
        return None
    return (uncached * r[0] + output * r[1] + cache_read * r[2] + cache_5m * r[3] + cache_1h * r[4]) / 1e6


# ----------------------------------------------------------------------------
# logging / config / env

def log(*parts):
    """One timestamped line on stderr (launchd sends it to ~/Library/Logs/claude-usage-dashboard.log)."""
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(stamp, " ".join(str(x) for x in parts), file=sys.stderr, flush=True)


def config_path(name):
    """First existing copy of `name` in Application Support, then next to server.py; else the ROOT path."""
    for d in (APP_SUPPORT_DIR, ROOT):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(ROOT, name)


def load_env():
    path = config_path(".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = config_path("config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


class DayClock:
    """Buckets timestamps into logical days that start at `start_hour` local time in `tz`."""

    def __init__(self, tz_name, start_hour):
        self.tz_name = tz_name
        self.tz = zoneinfo.ZoneInfo(tz_name)
        self.start_hour = int(start_hour)
        self.shift = dt.timedelta(hours=self.start_hour)

    def marker(self):
        return f"{self.tz_name}|{self.start_hour}"

    def day_of(self, t):
        """Logical day (date) containing aware datetime `t`."""
        return (t.astimezone(self.tz) - self.shift).date()

    def day_of_iso(self, ts):
        """Logical day as 'YYYY-MM-DD' for an ISO-8601 string; '' if unparseable."""
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return ""
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return self.day_of(t).isoformat()

    def day_start(self, d):
        """Aware datetime (UTC) at which logical day `d` begins."""
        local = dt.datetime.combine(d, dt.time(hour=self.start_hour), tzinfo=self.tz)
        return local.astimezone(dt.timezone.utc)

    def today(self):
        return self.day_of(utcnow())


def iso(d):
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Anthropic Admin API client

class AdminAPIError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class AdminAPI:
    def __init__(self, key):
        self.key = key

    def get(self, path, params, beta=None):
        q = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                q.extend((k + "[]", x) for x in v)
            else:
                q.append((k, v))
        url = API_BASE + path + "?" + urllib.parse.urlencode(q)
        headers = {
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
            "User-Agent": USER_AGENT,
        }
        if beta:
            headers["anthropic-beta"] = beta
        for attempt in range(4):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 429 or e.code >= 500:
                    wait = float(e.headers.get("retry-after") or (2 ** attempt))
                    time.sleep(min(wait, 30))
                    continue
                raise AdminAPIError(e.code, body)
        raise AdminAPIError(429, "rate limited after retries")

    def paginate(self, path, params, beta=None, page_key="page", next_key="next_page"):
        params = dict(params)
        while True:
            data = self.get(path, params, beta)
            yield data
            if not data.get("has_more") or not data.get(next_key):
                return
            params[page_key] = data[next_key]

    # --- name lookups
    def list_api_keys(self):
        out = {}
        params = {"limit": 1000}
        while True:
            data = self.get("/v1/organizations/api_keys", params)
            for k in data.get("data", []):
                ws = k.get("workspace_id") or (k.get("scope") or {}).get("workspace_id")
                out[k["id"]] = {"name": k.get("name") or k["id"], "workspace_id": ws,
                                "status": k.get("status"), "hint": k.get("partial_key_hint")}
            if not data.get("has_more") or not data.get("last_id"):
                return out
            params["after_id"] = data["last_id"]

    def list_workspaces(self):
        out = {}
        params = {"limit": 100}
        while True:
            data = self.get("/v1/organizations/workspaces", params)
            for w in data.get("data", []):
                out[w["id"]] = w.get("name") or w["id"]
            if not data.get("has_more") or not data.get("last_id"):
                return out
            params["after_id"] = data["last_id"]

    # --- Usage and Cost Analytics API (recommended; refreshed ~every 4h)
    def analytics(self, kind, start, end, group_by):
        path = f"/v1/organizations/analytics/{kind}"
        params = {
            "starting_at": iso(start), "ending_at": iso(end),
            "time_bucket_width": "1h", "group_by": group_by,
            "order_by": "time", "order": "asc", "limit": 1000,
        }
        rows, refreshed = [], None
        for attempt in range(2):
            try:
                rows, refreshed = [], None
                for page in self.paginate(path, params, beta=ANALYTICS_BETA):
                    rows.extend(page.get("data", []))
                    refreshed = page.get("data_refreshed_at") or refreshed
                break
            except AdminAPIError as e:
                if e.status == 410 and attempt == 0:   # cursor expired, restart
                    continue
                raise
        return rows, refreshed

    # --- legacy usage report (fresh within ~5 min; tokens only)
    def legacy_usage(self, start, end, group_by, bucket_width="1d"):
        # Note: with 1d buckets the report never includes the current (incomplete) UTC day;
        # 1h buckets do include today's completed hours.
        params = {"starting_at": iso(start), "ending_at": iso(end), "bucket_width": bucket_width,
                  "group_by": group_by, "limit": {"1d": 31, "1h": 168, "1m": 1440}[bucket_width]}
        buckets = []
        for page in self.paginate("/v1/organizations/usage_report/messages", params):
            buckets.extend(page.get("data", []))
        return buckets


# ----------------------------------------------------------------------------
# normalization helpers

def row_key(g):
    return (g.get("api_key_id"), g.get("model"), g.get("product"), g.get("workspace_id"))


def new_row(api_key_id, model, product, workspace_id):
    return {"api_key_id": api_key_id, "model": model, "product": product, "workspace_id": workspace_id,
            "uncached": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "output": 0,
            "requests": 0, "web_search": 0, "cost_usd": None, "list_usd": None, "cost_by_type": {}}


def add_usage(row, u):
    cc = u.get("cache_creation") or {}
    row["uncached"] += u.get("uncached_input_tokens", 0) or 0
    row["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
    row["cache_5m"] += cc.get("ephemeral_5m_input_tokens", 0) or 0
    row["cache_1h"] += cc.get("ephemeral_1h_input_tokens", 0) or 0
    row["output"] += u.get("output_tokens", 0) or 0
    row["requests"] += u.get("requests") or 0
    row["web_search"] += (u.get("server_tool_use") or {}).get("web_search_requests", 0) or 0


# ----------------------------------------------------------------------------
# the store: everything the dashboard needs, refreshed by background threads

class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        os.makedirs(CACHE_DIR, exist_ok=True)
        key = os.environ.get("ANTHROPIC_ADMIN_KEY") or ""
        self.api = AdminAPI(key) if key else None
        self.status = {
            "has_admin_key": bool(key),
            "analytics": {"last_ok": None, "last_error": None, "data_refreshed_at": None, "running": False,
                          "mode": "analytics"},
            "live": {"last_ok": None, "last_error": None, "running": False},
            "names": {"last_ok": None, "last_error": None},
            "local": {"last_ok": None, "last_error": None, "files": 0, "running": False},
            "app_logs": {"last_ok": None, "last_error": None, "files": 0, "running": False},
        }
        self.clock = DayClock(cfg["timezone"], cfg["day_start_hour"])
        # Both caches are keyed by logical day, so they are only valid for one day boundary.
        self.analytics_days = self._read_versioned("analytics_days.json")
        self._local_file_cache = self._read_versioned("local_files_v4.json")
        self._app_log_cache = self._read_versioned("app_logs.json")
        self.names = read_json(os.path.join(CACHE_DIR, "names.json"), {"api_keys": {}, "workspaces": {}})
        self.live_days = {}
        self.local_days = {}
        self.app_log_days = {}

    def _read_versioned(self, name):
        data = read_json(os.path.join(CACHE_DIR, name), {})
        if data.get("_clock") != self.clock.marker():
            return {}
        return data.get("data", {})

    def _write_versioned(self, name, data):
        write_json(os.path.join(CACHE_DIR, name), {"_clock": self.clock.marker(), "data": data})

    # ---------------- Analytics API
    def refresh_names(self):
        if not self.api:
            return
        try:
            keys = self.api.list_api_keys()
            try:
                wss = self.api.list_workspaces()
            except AdminAPIError as e:
                wss = self.names.get("workspaces", {})
                log("workspaces list failed:", e)
            with self.lock:
                self.names = {"api_keys": keys, "workspaces": wss}
                self.status["names"].update(last_ok=iso(utcnow()), last_error=None)
            write_json(os.path.join(CACHE_DIR, "names.json"), self.names)
        except Exception as e:
            with self.lock:
                self.status["names"]["last_error"] = str(e)
            log("names refresh failed:", e)

    def _names_for_unknown_keys(self, rows):
        """Re-fetch key names at once if usage rows mention a key id we have no name for
        (e.g. a key created since the last 6-hourly refresh). At most once per 10 minutes."""
        with self.lock:
            known = self.names.get("api_keys", {})
            unknown = {r.get("api_key_id") for r in rows if r.get("api_key_id") and r["api_key_id"] not in known}
            if not unknown:
                return
            last = getattr(self, "_unknown_key_refresh_at", 0)
            if time.time() - last < 600:
                return
            self._unknown_key_refresh_at = time.time()
        log("unknown API key id(s) in usage data, refreshing names:", ", ".join(sorted(unknown)))
        self.refresh_names()

    def _dates_to_fetch(self):
        today = self.clock.today()
        start = max(today - dt.timedelta(days=self.cfg["history_days"]),
                    ANALYTICS_EARLIEST, today - dt.timedelta(days=364))
        revisit_from = today - dt.timedelta(days=self.cfg["revisit_days"])
        want = []
        d = start
        while d <= today:
            s = d.isoformat()
            if s not in self.analytics_days or d >= revisit_from:
                want.append(d)
            d += dt.timedelta(days=1)
        # group into contiguous windows of <= 31 days
        windows = []
        for d in want:
            if windows and (d - windows[-1][1]).days == 1 and (d - windows[-1][0]).days < 30:
                windows[-1][1] = d
            else:
                windows.append([d, d])
        return windows

    def refresh_analytics(self):
        if not self.api:
            return
        with self.lock:
            self.status["analytics"]["running"] = True
        try:
            group_by = ["api_key_id", "model", "product", "workspace_id"]
            refreshed_at = None
            for w_start, w_end in self._dates_to_fetch():
                # Hourly buckets from the start of the first logical day to the start of the day
                # after the last one (ending at "now" would drop the current partial bucket).
                start = self.clock.day_start(w_start)
                end = self.clock.day_start(w_end + dt.timedelta(days=1))
                if self.status["analytics"].get("mode") == "legacy":
                    usage_rows, cost_rows, r1, r2 = self._legacy_window(start, end), [], None, None
                else:
                    try:
                        usage_rows, r1 = self.api.analytics("usage", start, end, group_by)
                        cost_rows, r2 = self.api.analytics("cost", start, end, group_by + ["cost_type", "token_type"])
                    except AdminAPIError as e:
                        if e.status not in (401, 403, 404):
                            raise
                        # Organization-scoped (non-admin) keys can read the legacy usage report but
                        # not the Analytics API. Fall back: tokens from the legacy report, cost estimated.
                        log(f"Analytics API unavailable ({e}); falling back to legacy usage report")
                        with self.lock:
                            self.status["analytics"]["mode"] = "legacy"
                        usage_rows, cost_rows, r1, r2 = self._legacy_window(start, end), [], None, None
                refreshed_at = r1 or r2 or refreshed_at
                days = {}
                d = w_start
                while d <= w_end:
                    days[d.isoformat()] = {}
                    d += dt.timedelta(days=1)
                for item in usage_rows:
                    day = self.clock.day_of_iso(item["starting_at"])
                    g = item.get("group") or {}
                    rows = days.setdefault(day, {})
                    k = row_key(g)
                    row = rows.get(k) or new_row(*k)
                    add_usage(row, item.get("usage") or {})
                    rows[k] = row
                for item in cost_rows:
                    day = self.clock.day_of_iso(item["starting_at"])
                    g = item.get("group") or {}
                    rows = days.setdefault(day, {})
                    k = row_key(g)
                    row = rows.get(k) or new_row(*k)
                    c = item.get("cost") or {}
                    amt = float(c.get("amount") or 0) / 100.0
                    lst = float(c.get("list_amount") or 0) / 100.0
                    row["cost_usd"] = (row["cost_usd"] or 0) + amt
                    row["list_usd"] = (row["list_usd"] or 0) + lst
                    t = g.get("token_type") or g.get("cost_type") or "other"
                    row["cost_by_type"][t] = row["cost_by_type"].get(t, 0) + amt
                    rows[k] = row
                if self.status["analytics"].get("mode") == "legacy":
                    for rows in days.values():
                        for row in rows.values():
                            row["cost_usd"] = estimate_cost(row["model"], row["uncached"], row["cache_read"],
                                                            row["cache_5m"], row["cache_1h"], row["output"])
                            row["estimated"] = True
                with self.lock:
                    for day, rows in days.items():
                        self.analytics_days[day] = {"rows": list(rows.values()), "fetched_at": iso(utcnow())}
                self._write_versioned("analytics_days.json", self.analytics_days)
                self._names_for_unknown_keys([r for rows in days.values() for r in rows.values()])
            with self.lock:
                st = self.status["analytics"]
                st.update(last_ok=iso(utcnow()), last_error=None)
                if refreshed_at:
                    st["data_refreshed_at"] = refreshed_at
        except Exception as e:
            with self.lock:
                self.status["analytics"]["last_error"] = str(e)
            log("analytics refresh failed:", e)
        finally:
            with self.lock:
                self.status["analytics"]["running"] = False

    def _legacy_window(self, start, end):
        """Legacy usage report for [start, end), hourly, reshaped like Analytics API usage rows."""
        items = []
        buckets = []
        s = start
        while s < end:   # hourly buckets are limited to 168 per request
            e = min(end, s + dt.timedelta(days=7))
            buckets.extend(self.api.legacy_usage(s, e, ["api_key_id", "model", "workspace_id"], bucket_width="1h"))
            s = e
        for b in buckets:
            for r in b.get("results", []):
                items.append({"starting_at": b["starting_at"],
                              "group": {"api_key_id": r.get("api_key_id"), "model": r.get("model"),
                                        "product": None, "workspace_id": r.get("workspace_id")},
                              "usage": r})
        return items

    # ---------------- legacy usage report for yesterday and today (live)
    def refresh_live(self):
        if not self.api:
            return
        with self.lock:
            self.status["live"]["running"] = True
        try:
            today = self.clock.today()
            start = self.clock.day_start(today - dt.timedelta(days=1))
            end = self.clock.day_start(today + dt.timedelta(days=1))
            buckets = self.api.legacy_usage(start, end, ["api_key_id", "model", "workspace_id"], bucket_width="1h")
            days = {}
            for b in buckets:
                day = self.clock.day_of_iso(b["starting_at"])
                rows = days.setdefault(day, {})
                for r in b.get("results", []):
                    k = (r.get("api_key_id"), r.get("model"), None, r.get("workspace_id"))
                    row = rows.get(k) or new_row(*k)
                    add_usage(row, r)
                    rows[k] = row
            for day, rows in days.items():
                for row in rows.values():
                    row["cost_usd"] = estimate_cost(row["model"], row["uncached"], row["cache_read"],
                                                    row["cache_5m"], row["cache_1h"], row["output"])
                    row["estimated"] = True
            with self.lock:
                self.live_days = {d: list(rows.values()) for d, rows in days.items()}
                self.status["live"].update(last_ok=iso(utcnow()), last_error=None)
            self._names_for_unknown_keys([r for rows in days.values() for r in rows.values()])
        except Exception as e:
            with self.lock:
                self.status["live"]["last_error"] = str(e)
            log("live refresh failed:", e)
        finally:
            with self.lock:
                self.status["live"]["running"] = False

    # ---------------- local Claude Code transcripts
    def refresh_local(self):
        with self.lock:
            self.status["local"]["running"] = True
        try:
            base = os.path.expanduser(self.cfg["claude_projects_dir"])
            files = glob.glob(os.path.join(base, "*", "*.jsonl"))
            cache = self._local_file_cache
            seen_paths = set()
            changed = False
            for path in files:
                seen_paths.add(path)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                sig = f"{st.st_size}:{int(st.st_mtime)}"
                if cache.get(path, {}).get("sig") == sig:
                    continue
                cache[path] = {"sig": sig, "agg": parse_transcript(path, self.clock)}
                changed = True
            for path in list(cache):
                if path not in seen_paths:
                    del cache[path]
                    changed = True
            if changed:
                self._write_versioned("local_files_v4.json", cache)
            # aggregate: day -> (project, model, entrypoint, billing) -> row
            days = {}
            for entry in cache.values():
                for a in entry["agg"]:
                    rows = days.setdefault(a["day"], {})
                    project = self.project_name(a.get("cwd"), a.get("entrypoint"))
                    billing = a.get("billing") or "subscription"
                    k = (project, a["model"], a.get("entrypoint"), billing)
                    row = rows.get(k)
                    if not row:
                        row = {"project": project, "model": a["model"], "entrypoint": a.get("entrypoint") or "cli",
                               "billing": billing,
                               "uncached": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "output": 0, "requests": 0}
                        rows[k] = row
                    for f in ("uncached", "cache_read", "cache_5m", "cache_1h", "output", "requests"):
                        row[f] += a[f]
            for rows in days.values():
                for row in rows.values():
                    row["cost_usd"] = estimate_cost(row["model"], row["uncached"], row["cache_read"],
                                                    row["cache_5m"], row["cache_1h"], row["output"])
            with self.lock:
                self.local_days = {d: list(r.values()) for d, r in days.items()}
                self.status["local"].update(last_ok=iso(utcnow()), last_error=None, files=len(files))
        except Exception as e:
            with self.lock:
                self.status["local"]["last_error"] = str(e)
            traceback.print_exc()
        finally:
            with self.lock:
                self.status["local"]["running"] = False

    GENERIC_DIRS = {"data", "state", "cache", "tmp", "var", "run", "work", "workspace"}

    def project_name(self, cwd, entrypoint=None):
        """Display name for a transcript's working directory.

        Interactive sessions are named after the folder. Agent SDK sessions are apps: they often
        run in a data folder inside the app, so a generic folder name falls back to its parent.
        `app_names` in config.json can override by full path or by path suffix.
        """
        cwd = (cwd or "").rstrip("/")
        if not cwd:
            return "unknown"
        overrides = self.cfg.get("app_names") or {}
        if cwd in overrides:
            return overrides[cwd]
        for suffix, name in overrides.items():
            if "/" in suffix and cwd.endswith("/" + suffix.strip("/")):
                return name
        if cwd == os.path.expanduser("~").rstrip("/"):
            return "home folder"   # Claude Code started in $HOME, not a project dir
        parts = cwd.split("/")
        name = parts[-1]
        if (entrypoint or "").startswith("sdk"):
            if cwd.startswith(("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")):
                return overrides.get("temp dir", "Ad hoc (temp dir)")   # one-off SDK runs from tests or scratch dirs
            if name in self.GENERIC_DIRS and len(parts) > 1:
                name = parts[-2]
        return overrides.get(name, name) or "unknown"

    # ---------------- per-call usage logs written by apps (~/.claude/app-usage/*.jsonl)
    def refresh_app_logs(self):
        with self.lock:
            self.status["app_logs"]["running"] = True
        try:
            base = os.path.expanduser(self.cfg["app_usage_dir"])
            files = glob.glob(os.path.join(base, "*.jsonl")) + glob.glob(os.path.join(base, "*", "*.jsonl"))
            cache = self._app_log_cache
            changed = False
            for path in files:
                try:
                    size = os.stat(path).st_size
                except OSError:
                    continue
                entry = cache.get(path) or {"offset": 0, "agg": {}}
                if size < entry["offset"]:            # truncated or rotated: start over
                    entry = {"offset": 0, "agg": {}}
                if size == entry["offset"]:
                    continue
                entry["offset"] = parse_app_log(path, entry["offset"], entry["agg"], self.clock)
                cache[path] = entry
                changed = True
            for path in list(cache):
                if path not in files:
                    del cache[path]
                    changed = True
            if changed:
                self._write_versioned("app_logs.json", cache)
            overrides = self.cfg.get("app_names") or {}
            days = {}
            for entry in cache.values():
                for key, a in entry["agg"].items():
                    day, app, model, source, billing = key.split("\t")
                    app = overrides.get(app, app)
                    rows = days.setdefault(day, {})
                    k = (app, model, source, billing)
                    row = rows.get(k)
                    if not row:
                        row = {"app": app, "model": model, "source": source, "billing": billing,
                               "uncached": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "output": 0, "requests": 0}
                        rows[k] = row
                    for f in ("uncached", "cache_read", "cache_5m", "cache_1h", "output", "requests"):
                        row[f] += a[f]
            for rows in days.values():
                for row in rows.values():
                    row["cost_usd"] = estimate_cost(row["model"], row["uncached"], row["cache_read"],
                                                    row["cache_5m"], row["cache_1h"], row["output"])
            with self.lock:
                self.app_log_days = {d: list(r.values()) for d, r in days.items()}
                self.status["app_logs"].update(last_ok=iso(utcnow()), last_error=None, files=len(files))
        except Exception as e:
            with self.lock:
                self.status["app_logs"]["last_error"] = str(e)
            traceback.print_exc()
        finally:
            with self.lock:
                self.status["app_logs"]["running"] = False

    # ---------------- snapshot for the UI
    def snapshot(self):
        with self.lock:
            keys = self.names.get("api_keys", {})
            wss = self.names.get("workspaces", {})
            overrides = self.cfg.get("app_names") or {}
            key_info = {}
            for kid, k in keys.items():
                display = overrides.get(kid) or overrides.get(k["name"]) or k["name"]
                key_info[kid] = {"name": k["name"], "display": display, "workspace_id": k.get("workspace_id"),
                                 "workspace": wss.get(k.get("workspace_id"), None), "status": k.get("status")}
            return {
                "generated_at": iso(utcnow()),
                "status": json.loads(json.dumps(self.status)),
                "config": {"history_days": self.cfg["history_days"], "pricing_models": sorted(PRICING),
                           "timezone": self.clock.tz_name, "day_start_hour": self.clock.start_hour},
                "today": self.clock.today().isoformat(),
                "keys": key_info,
                "workspaces": wss,
                "api_days": {d: v["rows"] for d, v in self.analytics_days.items()},
                "live_days": self.live_days,
                "local_days": self.local_days,
                "app_log_days": self.app_log_days,
            }

    # ---------------- schedulers
    def start(self):
        def loop(fn, interval_s, first_delay=0):
            def run():
                time.sleep(first_delay)
                while True:
                    try:
                        fn()
                    except Exception:
                        log(f"{fn.__name__} crashed:\n{traceback.format_exc()}")
                    time.sleep(interval_s)
            threading.Thread(target=run, daemon=True).start()

        loop(self.refresh_local, self.cfg["local_refresh_sec"])
        loop(self.refresh_app_logs, self.cfg["local_refresh_sec"], first_delay=3)
        if self.api:
            loop(self.refresh_names, 6 * 3600)
            loop(self.refresh_live, self.cfg["live_refresh_min"] * 60, first_delay=1)
            loop(self.refresh_analytics, self.cfg["analytics_refresh_min"] * 60, first_delay=2)

    def refresh_all_async(self):
        for fn in (self.refresh_names, self.refresh_live, self.refresh_analytics, self.refresh_local,
                   self.refresh_app_logs):
            threading.Thread(target=fn, daemon=True).start()


def parse_transcript(path, clock):
    """Aggregate one Claude Code session transcript into (day, project, model) token sums.

    Streaming writes one record per content block with the same message id and the
    same (final) usage, so usage is counted once per message id.

    Billing: Claude Code appends a {"type": "attachment", "attachment": {"type": "credential_org"}}
    record when (and only when) the session runs on a claude.ai login, so a transcript with one
    was billed to the subscription and one without ran on an API key (which the Analytics API
    already reports). Claude Code older than 2.1.281 never wrote the record; those sessions are
    assumed to be on the subscription.
    """
    agg = {}
    seen = set()
    cwd = None
    entrypoint = None
    credential_org = None
    version = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"credential_org"' in line and credential_org is None:
                    try:
                        att = (json.loads(line).get("attachment") or {})
                    except ValueError:
                        att = {}
                    if att.get("type") == "credential_org":
                        credential_org = att.get("organizationUuid") or "unknown"
                    continue
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                if not u:
                    continue
                model = m.get("model") or d.get("requestedModel") or "unknown"
                if model == "<synthetic>":
                    continue
                mid = m.get("id") or d.get("uuid")
                if mid in seen:
                    continue
                seen.add(mid)
                if cwd is None and d.get("cwd"):
                    cwd = d["cwd"]
                if entrypoint is None and d.get("entrypoint"):
                    entrypoint = d["entrypoint"]   # cli (interactive) / sdk-py / sdk-ts / sdk-cli
                if version is None and d.get("version"):
                    version = d["version"]
                ts = d.get("timestamp") or ""
                day = clock.day_of_iso(ts) or "unknown"
                k = (day, model)
                row = agg.get(k)
                if not row:
                    row = {"day": day, "model": model, "uncached": 0, "cache_read": 0,
                           "cache_5m": 0, "cache_1h": 0, "output": 0, "requests": 0}
                    agg[k] = row
                cc = u.get("cache_creation") or {}
                cw = u.get("cache_creation_input_tokens", 0) or 0
                c5, c1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
                if c5 is None and c1 is None:
                    c5, c1 = cw, 0   # older records: assume 5m TTL
                row["uncached"] += u.get("input_tokens", 0) or 0
                row["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
                row["cache_5m"] += c5 or 0
                row["cache_1h"] += c1 or 0
                row["output"] += u.get("output_tokens", 0) or 0
                row["requests"] += 1
    except OSError:
        return []
    for row in agg.values():
        row["cwd"] = cwd
        row["entrypoint"] = entrypoint or "cli"
        row["billing"] = "api" if (credential_org is None and _version_tuple(version) >= (2, 1, 281)) else "subscription"
    return list(agg.values())


def _version_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)
    except ValueError:
        return (0,)


def parse_app_log(path, offset, agg, clock):
    """Fold new lines of an app usage log (from byte `offset`) into `agg`; returns the new offset.

    One JSON object per line: {"ts": ISO-8601, "app": name, "model": id, "source": "agent-sdk"|"api",
    "billing": "subscription"|"api", "usage": {...Anthropic usage fields...}}. Lines that do not
    parse, or are only partly written, are skipped; a partial last line is re-read next time.
    """
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return offset
    for raw in data[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        u = d.get("usage") or {}
        if not isinstance(u, dict):
            continue
        day = clock.day_of_iso(d.get("ts") or "") or "unknown"
        key = "\t".join([day, str(d.get("app") or "unknown"), str(d.get("model") or "unknown"),
                         str(d.get("source") or "agent-sdk"), str(d.get("billing") or "subscription")])
        row = agg.get(key)
        if not row:
            row = agg[key] = {"uncached": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "output": 0, "requests": 0}
        cc = u.get("cache_creation") or {}
        c5, c1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
        if c5 is None and c1 is None:
            c5, c1 = u.get("cache_creation_input_tokens", 0) or 0, 0
        row["uncached"] += int(u.get("input_tokens", 0) or 0)
        row["cache_read"] += int(u.get("cache_read_input_tokens", 0) or 0)
        row["cache_5m"] += int(c5 or 0)
        row["cache_1h"] += int(c1 or 0)
        row["output"] += int(u.get("output_tokens", 0) or 0)
        row["requests"] += 1
    return offset + end + 1


# ----------------------------------------------------------------------------
# HTTP

class Handler(BaseHTTPRequestHandler):
    store = None

    def log_message(self, fmt, *args):   # quieter logs, same timestamp format as everything else
        if "/api/" not in (args[0] if args else ""):
            log(self.address_string(), fmt % args)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/summary":
            body = json.dumps(self.store.snapshot()).encode()
            return self._send(200, body, "application/json")
        if path == "/api/refresh":
            self.store.refresh_all_async()
            return self._send(200, b'{"ok":true}', "application/json")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        return self.do_GET()


def self_test(store):
    """Run every refresh once, synchronously, and print what each source produced.

    Does not open a port, so it is safe to run while the launchd instance is up (both only ever
    write identical cache files atomically). Exit status 1 if any source reported an error.
    """
    jobs = [("local", store.refresh_local), ("app_logs", store.refresh_app_logs)]
    if store.api:
        jobs += [("names", store.refresh_names), ("live", store.refresh_live), ("analytics", store.refresh_analytics)]
    else:
        log("ANTHROPIC_ADMIN_KEY not set: skipping the API sources")
    for name, fn in jobs:
        t0 = time.time()
        fn()
        st = store.status[name]
        extra = f", {st['files']} files" if "files" in st else ""
        log(f"{name}: {'ok' if not st.get('last_error') else 'ERROR ' + str(st['last_error'])}"
            f" ({time.time() - t0:.1f}s{extra})")
    snap = store.snapshot()
    log(f"snapshot: {len(json.dumps(snap))} bytes, keys={sorted(snap)}")
    failed = [n for n, _ in jobs if store.status[n].get("last_error")]
    log("SELF-TEST " + ("FAILED: " + ", ".join(failed) if failed else "OK"))
    return 1 if failed else 0


def main():
    load_env()
    cfg = load_config()
    port = int(os.environ.get("PORT") or cfg["port"])
    store = Store(cfg)
    if "--test" in sys.argv[1:]:
        sys.exit(self_test(store))
    if not store.api:
        log("ANTHROPIC_ADMIN_KEY not set: API usage disabled, showing local Claude Code transcripts only.")
    store.start()
    Handler.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"Claude usage dashboard: http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
