import asyncio
import time
import json
import ssl
import socket
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, delete
from .database import Base, engine, AsyncSessionLocal, DATABASE_URL
from .migrations import migrate
from .models import Monitor, Check, StateEvent, QueryStatus, QueryFailure

# --- Configuration ---
CHECK_INTERVAL = 60  # seconds between checks (liveness poll cadence)
ENDPOINTS = [
    "https://arax.ci.transltr.io",
    "https://arax.test.transltr.io",
    "https://arax.transltr.io",
    "https://arax.ncats.io",
    "https://arax.ncats.io/test",
    "https://arax.ncats.io/shepherd",
    "https://arax.ncats.io/beta",
    "https://arax.ncats.io/devED",
    "https://arax.ncats.io/devLM",
]

# ARAX endpoints are monitored via the ARAX status API rather than a plain root
# GET: hitting the status route proves the Flask backend is alive (a root GET
# only proves the static frontend / reverse proxy is up). The same response
# yields the build version (the `tier0-YYYYMMDD` token from curie_to_pmids_version).
ARAX_ENDPOINTS = {
    "https://arax.ci.transltr.io",
    "https://arax.test.transltr.io",
    "https://arax.transltr.io",
    "https://arax.ncats.io",
    "https://arax.ncats.io/test",
    "https://arax.ncats.io/shepherd",
    "https://arax.ncats.io/beta",
    "https://arax.ncats.io/devED",
    "https://arax.ncats.io/devLM",
}
# ARAX API version in each node's URL path (/api/arax/<version>/...). Nodes not
# listed use ARAX_DEFAULT_API_VERSION; devED runs the TRAPI 2.0 / FastAPI build.
ARAX_DEFAULT_API_VERSION = "v1.4"
ARAX_API_VERSIONS = {
    "https://arax.ncats.io/devED": "v2.0",
}


def arax_api(url: str) -> str:
    return f"{url}/api/arax/{ARAX_API_VERSIONS.get(url, ARAX_DEFAULT_API_VERSION)}"


def arax_status_url(url: str) -> str:
    return arax_api(url) + "/status?mode=site_config"


# ARAX nodes whose status API is broken (returns 500 while the site and its
# queries work): liveness falls back to a plain root GET. Their build version is
# still tried via the status API, throttled, so it reappears once that's fixed.
ROOT_LIVENESS_ENDPOINTS = {
    "https://arax.transltr.io",
}

# Once per QUERY_INTERVAL each ARAX node gets real TRAPI reasoning queries that
# check the reasoner actually works: a 200 with a non-empty message.results
# passes; anything else (error, timeout, no results) fails.
#
# Every ARAX node gets the "lookup" query (run inline; it is also the latency
# sample). FULL_PROBE_ENDPOINTS additionally get xDTD, xCRG and Pathfinder, run
# sequentially in a background task because they can take minutes.
#
# Health (see compute_health):
#   red    — status API down, or a full-probe node failing ALL of its queries
#   yellow — status API up but at least one query failing
#   green  — status API up and every query passing
#
# A query only counts as failing after QUERY_FAIL_THRESHOLD consecutive
# failures; while failing (or awaiting confirmation) it's re-checked every
# QUERY_RETRY_INTERVAL instead of hourly.
QUERY_INTERVAL = 3600  # seconds between /query probes of a passing query (1/hour)
QUERY_RETRY_INTERVAL = 300  # seconds between probes of a failing query
QUERY_FAIL_THRESHOLD = 2  # consecutive failures before a query counts as failing
# A result older than this is shown as stale (grey) — e.g. the node was down,
# so its queries couldn't run.
QUERY_STALE_AFTER = 2 * QUERY_INTERVAL
RESPONSE_SNIPPET_MAX = 8000  # chars of a failed response kept for the failure log
QUERY_TIMEOUTS = {  # seconds; inferred queries routinely take a minute or more
    "lookup": 60.0,
    "xdtd": 300.0,
    "xcrg": 300.0,
    "pathfinder": 300.0,
}
QUERY_LABELS = {"lookup": "Lookup", "xdtd": "xDTD", "xcrg": "xCRG", "pathfinder": "Pathfinder"}
FULL_PROBE_ENDPOINTS = {
    "https://arax.ci.transltr.io",
    "https://arax.test.transltr.io",
    "https://arax.ncats.io",
}
QUERY_GRAPHS = {
    "lookup": {
        "edges": {
            "e00": {
                "subject": "n00",
                "object": "n01",
                "predicates": ["biolink:interacts_with"],
            }
        },
        "nodes": {
            "n00": {"ids": ["CHEBI:46195"]},
            "n01": {"categories": ["biolink:Protein"]},
        },
    },
    "xdtd": {
        "edges": {
            "t_edge": {
                "attribute_constraints": [],
                "knowledge_type": "inferred",
                "object": "on",
                "predicates": ["biolink:treats"],
                "qualifier_constraints": [],
                "subject": "sn",
            }
        },
        "nodes": {
            "on": {
                "categories": ["biolink:Disease"],
                "constraints": [],
                "ids": ["MONDO:0015564"],
                "is_set": False,
            },
            "sn": {
                "categories": ["biolink:ChemicalEntity"],
                "constraints": [],
                "is_set": False,
            },
        },
    },
    "xcrg": {
        "edges": {
            "t_edge": {
                "knowledge_type": "inferred",
                "object": "on",
                "predicates": ["biolink:affects"],
                "qualifier_constraints": [
                    {
                        "qualifier_set": [
                            {
                                "qualifier_type_id": "biolink:object_aspect_qualifier",
                                "qualifier_value": "activity_or_abundance",
                            },
                            {
                                "qualifier_type_id": "biolink:object_direction_qualifier",
                                "qualifier_value": "increased",
                            },
                        ]
                    }
                ],
                "subject": "sn",
            }
        },
        "nodes": {
            "on": {"categories": ["biolink:Gene"], "ids": ["NCBIGene:1576"]},
            "sn": {"categories": ["biolink:ChemicalEntity"]},
        },
    },
    "pathfinder": {
        "nodes": {
            "n0": {"ids": ["MONDO:0005011"]},
            "n1": {"ids": ["MONDO:0005180"]},
        },
        "paths": {
            "p0": {
                "subject": "n0",
                "object": "n1",
                "predicates": ["biolink:related_to"],
            }
        },
    },
}
# Monotonic time each (monitor_id, kind) probe is next due; missing = due now.
_next_probe: dict[tuple[int, str], float] = {}
# Monitors with a background xDTD/xCRG/Pathfinder run in progress.
_extended_inflight: set[int] = set()
# Strong refs to in-flight background probe tasks so they aren't GC'd mid-run.
_probe_tasks: set[asyncio.Task] = set()
# Confirmed liveness after each monitor's last check, to spot DOWN -> UP.
_last_status_up: dict[int, bool] = {}
# Last health each monitor was alerted as; Slack only hears about changes.
_alerted_health: dict[int, str | None] = {}
# The queries that were failing when each monitor's last alert went out; the
# next alert reports what changed relative to this. Missing = unknown (e.g.
# after a restart), in which case it's re-learned silently.
_alerted_failing: dict[int, set[str]] = {}
# Serializes health updates per monitor (run_check and background probes both
# write them).
_health_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def query_kinds_for(url: str) -> list[str]:
    if url not in ARAX_ENDPOINTS:
        return []
    return list(QUERY_LABELS) if url in FULL_PROBE_ENDPOINTS else ["lookup"]

import os
import re
from urllib.parse import urlparse

from fastapi.templating import Jinja2Templates

# TLS verification is ON by default so an expired or broken certificate surfaces
# as DOWN (usually what you want from an uptime monitor). Set TLS_VERIFY=false to
# monitor hosts that serve self-signed or internal certificates.
TLS_VERIFY = os.getenv("TLS_VERIFY", "true").strip().lower() not in ("0", "false", "no", "off")
http_client = httpx.AsyncClient(timeout=10, verify=TLS_VERIFY)

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")
SLACK_WEBHOOK_ARAX = os.getenv("SLACK_WEBHOOK_URL_ARAX")

# Alerts for these hosts are additionally mirrored to the dedicated ARAX Slack
# channel (they still post to the default channel too).
ARAX_ALERT_HOSTS = {
    "arax.ci.transltr.io",
    "arax.test.transltr.io",
    "arax.transltr.io",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Build-metadata refresh throttle: re-fetch {url}/code_version at most this often
# per monitor, and never while holding a DB session open.
CODE_VERSION_REFRESH = 300  # seconds
_last_code_version_fetch: dict[int, float] = {}

# How long to keep raw per-check samples; older rows are pruned. Uptime history
# lives in StateEvent (never pruned), so this does not affect uptime fidelity.
RETENTION_DAYS = 35
PRUNE_INTERVAL = 6 * 3600  # seconds

# TLS certificate expiry is refreshed at most this often per monitor and cached
# in memory (re-checked shortly after each restart).
CERT_REFRESH = 6 * 3600  # seconds
_last_cert_fetch: dict[int, float] = {}
_cert_expiry: dict[int, int | None] = {}

def parse_build_metadata(description: str):
    build_dt = None
    biolink = None
    dataset_version = None

    # Build datetime
    m = re.search(r"done on ([0-9\-:\. ]+)", description)
    if m:
        build_dt = m.group(1)[:10]

    # Biolink version
    m = re.search(r"Biolink version used was ([0-9\.]+)", description)
    if m:
        biolink = m.group(1)

    # KG2 pattern (kg2c-2.10.2-v1.0)
    m = re.search(r"kg2c-([\d\.]+-v[\d\.]+)", description)
    if m:
        dataset_version = m.group(1)

    # Multiomics pattern (_v3.1.34.tsv or _v0.5.2.tsv etc)
    if not dataset_version:
        m = re.search(r"_v([\d\.]+)\.tsv", description)
        if m:
            dataset_version = m.group(1)

    return build_dt, biolink, dataset_version

async def post_to_webhook(webhook: str | None, text: str):
    if not webhook:
        return
    try:
        await http_client.post(webhook, json={"text": text})
    except Exception:
        pass

async def send_slack_message(text: str, url: str | None = None):
    # The default channel always gets the alert.
    await post_to_webhook(SLACK_WEBHOOK, text)
    # ARAX hosts are additionally mirrored to the dedicated ARAX channel.
    if url is not None:
        host = (urlparse(url).hostname or "").lower()
        if host in ARAX_ALERT_HOSTS:
            await post_to_webhook(SLACK_WEBHOOK_ARAX, text)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Add new columns to an existing DB in place (backing it up first), then
    # create any brand-new tables.
    db_path = DATABASE_URL.split("///", 1)[1]
    if os.path.exists(db_path):
        for change in await asyncio.to_thread(migrate, db_path):
            print(f"[MIGRATE] {change}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Monitor))
        monitors = result.scalars().all()

        existing_urls = {m.url for m in monitors}

        # Add missing
        for url in ENDPOINTS:
            if url not in existing_urls:
                session.add(Monitor(
                    url=url,
                    interval_seconds=CHECK_INTERVAL,
                    is_up=None,
                    last_state_change_ts=None
                ))

        # Seed from the DB so a restart doesn't re-announce current states.
        for m in monitors:
            _alerted_health[m.id] = m.health

        # Monitors no longer in ENDPOINTS are kept (with their history) but are
        # no longer checked or shown on the dashboard.
        await session.commit()

    checker_task = asyncio.create_task(checker_loop())
    prune_task = asyncio.create_task(prune_loop())
    yield
    checker_task.cancel()
    prune_task.cancel()
    await http_client.aclose()

app = FastAPI(lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allow all headers
)

def format_duration_str(seconds):
    """Compact, seconds-free duration: highest non-zero unit down through
    minutes, capped at 3 units (e.g. '5d 3h 20m', '2h 15m', '1y 2mo 5d')."""
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    units = [("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)]
    parts = []
    rem = seconds
    for label, size in units:
        value = rem // size
        if value > 0 or parts:
            parts.append(f"{value}{label}")
            rem -= value * size
        if len(parts) == 3:
            break
    return " ".join(parts) if parts else "<1m"


def _bucket_boundaries(now_ts, tz, count, unit):
    """`count` consecutive (start_ts, end_ts, label) buckets ending with the
    one containing now. unit is 'hour' (clock-hour aligned) or 'day' (midnight
    aligned, tz-aware)."""
    now_local = datetime.fromtimestamp(now_ts, tz)
    if unit == "hour":
        anchor = now_local.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
        fmt = "%-I %p"
    else:  # day
        anchor = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)
        fmt = "%a, %b %-d"
    out = []
    for i in range(count - 1, -1, -1):
        start_local = anchor - i * step
        end_local = start_local + step
        out.append((int(start_local.timestamp()), int(end_local.timestamp()),
                    start_local.strftime(fmt)))
    return out


def event_status(e) -> str:
    """up|degraded|down for a StateEvent (rows predating `status` only knew
    up/down)."""
    return e.status or ("up" if e.is_up else "down")


def compute_uptime_buckets(events, now_ts, tz, count, unit):
    """Reconstruct per-bucket health over the last `count` buckets from state
    events. A bucket takes the worst state seen in it: any downtime -> down
    (red), else any degraded time -> degraded (yellow), else up. Only downtime
    counts against uptime %. Returns a list (oldest first) of dicts:
        {date, status: up|down|degraded|nodata, uptime: float|None,
         down_seconds, degraded_seconds, up_seconds, total_seconds}
    """
    evs = sorted(events, key=lambda e: e.changed_at_ts)
    monitoring_start = evs[0].changed_at_ts if evs else None

    # Contiguous state intervals [start, end) with their status; the last
    # event runs to now.
    intervals = []
    for i, e in enumerate(evs):
        start = e.changed_at_ts
        end = evs[i + 1].changed_at_ts if i + 1 < len(evs) else now_ts
        if end > start:
            intervals.append((start, end, event_status(e)))

    result = []
    for b_start, b_end, label in _bucket_boundaries(now_ts, tz, count, unit):
        eff_end = min(b_end, now_ts)
        eff_start = b_start if monitoring_start is None else max(b_start, monitoring_start)

        if monitoring_start is None or eff_end <= eff_start:
            result.append({"date": label, "status": "nodata", "uptime": None,
                           "down_seconds": 0, "degraded_seconds": 0,
                           "up_seconds": 0, "total_seconds": 0})
            continue

        total = eff_end - eff_start
        secs = {"up": 0, "degraded": 0, "down": 0}
        for s, e_, st in intervals:
            overlap = min(e_, eff_end) - max(s, eff_start)
            if overlap > 0:
                secs[st] = secs.get(st, 0) + overlap
        down, degraded = secs["down"], secs["degraded"]
        up_sec = total - down
        status = "down" if down > 0 else ("degraded" if degraded > 0 else "up")
        result.append({"date": label, "status": status,
                       "uptime": round(up_sec / total * 100, 3),
                       "down_seconds": int(down), "degraded_seconds": int(degraded),
                       "up_seconds": int(up_sec), "total_seconds": int(total)})
    return result


def compute_daily_status(events, now_ts, tz, days=30):
    return compute_uptime_buckets(events, now_ts, tz, days, "day")


def overall_uptime(daily):
    """Time-weighted uptime percent across the daily buckets, or None."""
    total = sum(d["total_seconds"] for d in daily)
    up = sum(d["up_seconds"] for d in daily)
    return round(up / total * 100, 3) if total > 0 else None


def _down_overlaps(events, now_ts, win_start):
    """Yield the seconds of downtime each DOWN interval contributes within
    [win_start, now_ts]."""
    evs = sorted(events, key=lambda e: e.changed_at_ts)
    for i, e in enumerate(evs):
        if e.is_up:
            continue
        start = e.changed_at_ts
        end = evs[i + 1].changed_at_ts if i + 1 < len(evs) else now_ts
        overlap = min(end, now_ts) - max(start, win_start)
        if overlap > 0:
            yield overlap


def uptime_over(events, now_ts, window_seconds):
    """Time-weighted uptime % over the last `window_seconds`, clamped to when
    monitoring began. None if there is no data yet."""
    evs = sorted(events, key=lambda e: e.changed_at_ts)
    if not evs:
        return None
    win_start = max(now_ts - window_seconds, evs[0].changed_at_ts)
    total = now_ts - win_start
    if total <= 0:
        return None
    down = sum(_down_overlaps(evs, now_ts, win_start))
    return round((total - down) / total * 100, 3)


def incident_summary(events, now_ts, window_seconds):
    """Outage count, total downtime, and longest outage within the window."""
    overlaps = list(_down_overlaps(events, now_ts, now_ts - window_seconds))
    return {
        "count": len(overlaps),
        "total_down": sum(overlaps),
        "longest": max(overlaps, default=0),
    }


def _get_cert_expiry_ts(url):
    """Epoch seconds when the TLS cert expires, or None. Blocking — call via
    asyncio.to_thread()."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    host, port = parsed.hostname, parsed.port or 443
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        not_after = cert.get("notAfter") if cert else None
        return int(ssl.cert_time_to_seconds(not_after)) if not_after else None
    except Exception:
        return None

FAILURE_LOG_DAYS = 7
FAILURE_LOG_MAX = 200  # most recent entries shown


def build_failure_log(failed_checks, query_failures, tz) -> list[dict]:
    """Failed status checks and failed /query probes, newest first. Repeats of
    the same failure close together (e.g. every poll during an outage) are
    collapsed into one entry showing the latest response."""
    items = []
    for c in failed_checks:
        dt = c.checked_at if c.checked_at.tzinfo else c.checked_at.replace(tzinfo=ZoneInfo("UTC"))
        summary = f"HTTP {c.status_code}" if c.status_code else (c.error_message or "no response")
        items.append((int(dt.timestamp()), "Status", summary, c.response_body or c.error_message,
                      2 * CHECK_INTERVAL + 30))
    for q in query_failures:
        items.append((q.checked_ts, QUERY_LABELS.get(q.kind, q.kind), q.error or "failed", q.response,
                      QUERY_RETRY_INTERVAL + QUERY_TIMEOUTS.get(q.kind, 60) + 60))
    items.sort(key=lambda i: i[0])

    groups = []
    last_by_source: dict[str, dict] = {}
    for ts, source, summary, response, max_gap in items:
        g = last_by_source.get(source)
        if g and g["summary"] == summary and ts - g["last_ts"] <= max_gap:
            g["count"] += 1
            g["last_ts"] = ts
            g["response"] = response or g["response"]
            continue
        g = {"source": source, "summary": summary, "response": response,
             "first_ts": ts, "last_ts": ts, "count": 1}
        groups.append(g)
        last_by_source[source] = g

    fmt = "%m/%d %I:%M %p"
    for g in groups:
        g["when"] = datetime.fromtimestamp(g["first_ts"], tz).strftime(fmt)
        if g["count"] > 1:
            g["when"] += " – " + datetime.fromtimestamp(g["last_ts"], tz).strftime(fmt)
    groups.sort(key=lambda g: g["last_ts"], reverse=True)
    return groups[:FAILURE_LOG_MAX]


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


def status_seconds(events, now_ts, window_seconds, status) -> int:
    """Seconds spent in `status` (up|degraded|down) over the last window."""
    evs = sorted(events, key=lambda e: e.changed_at_ts)
    win_start = now_ts - window_seconds
    total = 0
    for i, e in enumerate(evs):
        if event_status(e) != status:
            continue
        end = evs[i + 1].changed_at_ts if i + 1 < len(evs) else now_ts
        total += max(0, min(end, now_ts) - max(e.changed_at_ts, win_start))
    return total


def last_incident(events, now_ts, tz) -> dict | None:
    """The most recent stretch spent not-up (degraded and/or down, back to
    back), or None if there's never been one. Its kind is the worst state
    reached; it's ongoing if the monitor hasn't returned to up since."""
    evs = sorted(events, key=lambda e: e.changed_at_ts)
    end_ts = None
    i = len(evs) - 1
    if i >= 0 and event_status(evs[i]) == "up":
        end_ts = evs[i].changed_at_ts
        i -= 1
    worst = None
    start_ts = None
    while i >= 0 and event_status(evs[i]) != "up":
        if worst != "down":
            worst = event_status(evs[i])
        start_ts = evs[i].changed_at_ts
        i -= 1
    if start_ts is None:
        return None
    ongoing = end_ts is None
    return {
        "when": datetime.fromtimestamp(start_ts, tz).strftime("%b %-d, %-I:%M %p"),
        "kind": "Down" if worst == "down" else "Degraded",
        "class": "down" if worst == "down" else "degraded",
        "duration": format_duration_str((now_ts if ongoing else end_ts) - start_ts),
        "ago": format_duration_str(now_ts - start_ts),
        "ongoing": ongoing,
    }


@app.get("/monitor/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail(request: Request, monitor_id: int):
    pacific = ZoneInfo("America/Los_Angeles")
    now_ts = int(time.time())

    async with AsyncSessionLocal() as session:
        monitor = await session.get(Monitor, monitor_id)
        if not monitor:
            raise HTTPException(status_code=404)

        all_events = (await session.execute(
            select(StateEvent)
            .where(StateEvent.monitor_id == monitor_id)
            .order_by(StateEvent.changed_at_ts.asc())
        )).scalars().all()

        query_rows = (await session.execute(
            select(QueryStatus).where(QueryStatus.monitor_id == monitor_id)
        )).scalars().all()

        log_since_dt = datetime.now(ZoneInfo("UTC")) - timedelta(days=FAILURE_LOG_DAYS)
        failed_checks = (await session.execute(
            select(Check)
            .where(Check.monitor_id == monitor_id, Check.checked_at >= log_since_dt,
                   Check.status_code != 200)
            .order_by(Check.checked_at.asc())
        )).scalars().all()
        query_failures = (await session.execute(
            select(QueryFailure)
            .where(QueryFailure.monitor_id == monitor_id,
                   QueryFailure.checked_ts >= int(log_since_dt.timestamp()))
            .order_by(QueryFailure.checked_ts.asc())
        )).scalars().all()

    queries = load_query_statuses(query_rows, monitor.url, pacific, now_ts)

    # 30-day daily buckets for the status-bar strip
    daily = compute_daily_status(all_events, now_ts, pacific, days=30)

    time_in_status_str = (
        format_duration_str(now_ts - monitor.last_state_change_ts)
        if monitor.last_state_change_ts is not None else "Pending"
    )

    # Uptime windows and 30-day incident summary (from StateEvents).
    up_24h = uptime_over(all_events, now_ts, 86400)
    up_7d = uptime_over(all_events, now_ts, 7 * 86400)
    up_30d = uptime_over(all_events, now_ts, 30 * 86400)
    incidents = incident_summary(all_events, now_ts, 30 * 86400)
    degraded_30d = status_seconds(all_events, now_ts, 30 * 86400, "degraded")
    cert_ts = _cert_expiry.get(monitor_id)
    cert_days = (cert_ts - now_ts) // 86400 if cert_ts else None

    if monitor.health is None:
        status_label, status_class = "INITIALIZING...", "pending"
    else:
        status_label, status_class = monitor.health.upper(), monitor.health

    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "monitor_url": monitor.url,
            "status_label": status_label,
            "status_class": status_class,
            "initialized": monitor.last_state_change_ts is not None,
            "time_in_status_str": time_in_status_str,
            "daily": daily,
            "uptime_24h": up_24h,
            "uptime_7d": up_7d,
            "uptime_30d": up_30d,
            "incident_count": incidents["count"],
            "total_down_str": "0m" if incidents["total_down"] == 0 else format_duration_str(incidents["total_down"]),
            "longest_down_str": "—" if incidents["longest"] == 0 else format_duration_str(incidents["longest"]),
            "degraded_30d_str": "0m" if degraded_30d == 0 else format_duration_str(degraded_30d),
            "last_incident": last_incident(all_events, now_ts, pacific),
            "cert_days": cert_days,
            "queries": queries,
            "failure_log": build_failure_log(failed_checks, query_failures, pacific),
            "failure_log_days": FAILURE_LOG_DAYS,
            "start_ts": monitor.last_state_change_ts or 0,
        },
    )


@app.get("/status")
async def status():
    pacific = ZoneInfo("America/Los_Angeles")
    now_ts = int(time.time())
    async with AsyncSessionLocal() as session:
        monitors = (await session.execute(select(Monitor))).scalars().all()
        all_events = (await session.execute(
            select(StateEvent).order_by(StateEvent.changed_at_ts.asc())
        )).scalars().all()
        all_query_rows = (await session.execute(select(QueryStatus))).scalars().all()

    events_by_monitor: dict[int, list] = {}
    for e in all_events:
        events_by_monitor.setdefault(e.monitor_id, []).append(e)
    queries_by_monitor: dict[int, list] = {}
    for q in all_query_rows:
        queries_by_monitor.setdefault(q.monitor_id, []).append(q)

    result = []
    for m in monitors:
        if m.url not in ENDPOINTS:
            continue  # retired monitor: history kept, no longer shown
        if m.last_state_change_ts is not None:
            change_str = datetime.fromtimestamp(m.last_state_change_ts, tz=pacific).strftime("%b %-d, %-I:%M %p %Z")
        else:
            change_str = "Pending"
        hourly = compute_uptime_buckets(events_by_monitor.get(m.id, []), now_ts, pacific, 24, "hour")
        result.append({
            "id": m.id,
            "url": m.url,
            "is_up": m.is_up,
            "health": m.health,
            "last_state_change_ts": m.last_state_change_ts or 0,
            "last_state_change_str": change_str,
            "code_version": m.code_version,
            "queries": load_query_statuses(queries_by_monitor.get(m.id, []), m.url, pacific, now_ts),
            "uptime_24h": overall_uptime(hourly),
            "bars": [{"date": d["date"], "status": d["status"], "uptime": d["uptime"],
                      "degraded_seconds": d["degraded_seconds"]} for d in hourly],
        })

    # Display in ENDPOINTS order regardless of DB insertion order (unknown URLs last).
    order = {u: i for i, u in enumerate(ENDPOINTS)}
    result.sort(key=lambda r: order.get(r["url"], len(order)))
    return result

@app.get("/api/monitor/{monitor_id}")
async def api_monitor_detail(monitor_id: int):
    pacific = ZoneInfo("America/Los_Angeles")
    now_ts = int(time.time())
    one_day_ago_dt = datetime.now(ZoneInfo("UTC")) - timedelta(hours=24)
    one_day_ago_ts = int(one_day_ago_dt.timestamp())

    async with AsyncSessionLocal() as session:
        monitor = await session.get(Monitor, monitor_id)
        if not monitor: 
            raise HTTPException(status_code=404, detail="Monitor not found")
        
        checks = (await session.execute(
            select(Check)
            .where(Check.monitor_id == monitor_id, Check.checked_at >= one_day_ago_dt)
            .order_by(Check.checked_at.desc())
        )).scalars().all()
        
        events = (await session.execute(
            select(StateEvent)
            .where(StateEvent.monitor_id == monitor_id, StateEvent.changed_at_ts >= one_day_ago_ts)
            .order_by(StateEvent.changed_at_ts.desc())
        )).scalars().all()

        query_rows = (await session.execute(
            select(QueryStatus).where(QueryStatus.monitor_id == monitor_id)
        )).scalars().all()

    # Calculate stats
    if monitor.last_state_change_ts is not None:
        time_in_status_sec = now_ts - monitor.last_state_change_ts
        time_in_status_str = format_duration_str(time_in_status_sec)
        
        chart_data = [c.response_time_ms for c in checks if c.response_time_ms is not None]
        avg_lat = round(sum(chart_data) / len(chart_data), 2) if chart_data else 0
        
        up_checks = [c for c in checks if c.status_code == 200]
        uptime_pct = round((len(up_checks) / len(checks)) * 100, 2) if checks else 0
        
        change_str = datetime.fromtimestamp(monitor.last_state_change_ts, tz=pacific).strftime("%m/%d %I:%M %p %Z")
    else:
        time_in_status_str = "Pending"
        avg_lat = 0
        uptime_pct = 0
        change_str = "Pending"

    # Recent checks (last 10)
    recent_checks = []
    for c in list(reversed(checks))[:10]:
        dt = c.checked_at if c.checked_at.tzinfo else c.checked_at.replace(tzinfo=ZoneInfo("UTC"))
        recent_checks.append({
            "timestamp": dt.astimezone(pacific).strftime('%m/%d %I:%M:%S %p %Z'),
            "status_code": c.status_code,
            "response_time_ms": c.response_time_ms
        })

    # Recent events (last 5)
    recent_events = []
    for e in events[:5]:
        event_dt = datetime.fromtimestamp(e.changed_at_ts, tz=pacific)
        recent_events.append({
            "timestamp": event_dt.strftime('%m/%d %I:%M:%S %p %Z'),
            "is_up": e.is_up,
            "status": event_status(e).upper()
        })

    return {
        "id": monitor.id,
        "url": monitor.url,
        "is_up": monitor.is_up,
        "health": monitor.health,
        "last_state_change_ts": monitor.last_state_change_ts or 0,
        "last_state_change_str": change_str,
        "code_version": monitor.code_version,
        "queries": load_query_statuses(query_rows, monitor.url, pacific, now_ts),
        "avg_latency_ms": avg_lat,
        "uptime_24h_percent": uptime_pct,
        "time_in_current_status": time_in_status_str,
        "recent_checks": recent_checks,
        "recent_events": recent_events
    }

async def checker_loop():
    while True:
        try:
            # Read the monitor list and release the session BEFORE running checks,
            # so we don't hold a read transaction open while run_check() writes.
            async with AsyncSessionLocal() as session:
                monitors = (await session.execute(select(Monitor))).scalars().all()
                targets = [(m.id, m.url) for m in monitors if m.url in ENDPOINTS]
            print(f"[CHECKER] Running checks for {len(targets)} monitors...")
            await asyncio.gather(*[run_check(mid, url) for mid, url in targets])
            print(f"[CHECKER] Checks completed")
        except Exception as e:
            print(f"[CHECKER ERROR] {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(CHECK_INTERVAL)


async def prune_loop():
    """Periodically delete raw check samples and query failures older than
    RETENTION_DAYS. Uptime
    history lives in StateEvent and is never pruned."""
    while True:
        try:
            cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(days=RETENTION_DAYS)
            async with AsyncSessionLocal() as session:
                res = await session.execute(delete(Check).where(Check.checked_at < cutoff))
                qres = await session.execute(
                    delete(QueryFailure).where(QueryFailure.checked_ts < int(cutoff.timestamp()))
                )
                await session.commit()
                if res.rowcount:
                    print(f"[PRUNE] Deleted {res.rowcount} checks older than {RETENTION_DAYS}d")
                if qres.rowcount:
                    print(f"[PRUNE] Deleted {qres.rowcount} query failures older than {RETENTION_DAYS}d")
        except Exception as e:
            print(f"[PRUNE ERROR] {type(e).__name__}: {e}")
        await asyncio.sleep(PRUNE_INTERVAL)


FAIL_THRESHOLD = 2  # require N consecutive failures before marking DOWN


def parse_arax_version(data: dict) -> str | None:
    """Pull the reportable version (the tier0-YYYYMMDD token from
    curie_to_pmids_version) out of an ARAX status site_config payload."""
    cfg = data.get("config", {}) if isinstance(data, dict) else {}
    m = re.search(r"tier0-\d{8}", cfg.get("curie_to_pmids_version", "") or "")
    if not m:
        return None
    arax_ver = cfg.get("arax_version")
    return f"{m.group(0)} (ARAX {arax_ver})" if arax_ver else m.group(0)


async def fetch_arax_version(status_url: str) -> str | None:
    try:
        r = await http_client.get(status_url, timeout=8.0, follow_redirects=True)
        if r.status_code != 200:
            return None
        return parse_arax_version(r.json())
    except Exception:
        return None


async def fetch_code_version(url: str) -> str | None:
    """Fetch a display version string for a monitor, or None. ARAX endpoints use
    the ARAX status API; everyone else uses {url}/code_version."""
    if url in ARAX_ENDPOINTS:
        return await fetch_arax_version(arax_status_url(url))
    try:
        cv = await http_client.get(f"{url}/code_version", timeout=5.0)
        if cv.status_code != 200:
            return None
        data = cv.json()
        build_nodes = data.get("endpoint_build_nodes", {})
        rows = []
        for name, node in build_nodes.items():
            desc = node.get("description", "")

            # code_version from the response key (kg2c) or parsed from the description (multiomics)
            code_ver = node.get("code_version")
            if not code_ver:
                m_code = re.search(r"_v([\d\.]+)\.tsv", desc)
                if m_code:
                    code_ver = m_code.group(1)
                m_code = re.search(r"kg2c-([\d\.]+-v[\d\.]+)", desc)
                if m_code:
                    code_ver = m_code.group(1)

            biolink = node.get("biolink_version")
            if not biolink:
                m_biolink = re.search(r"Biolink version used was ([0-9\.]+)", desc)
                if m_biolink:
                    biolink = m_biolink.group(1)

            build_dt = None
            m_date = re.search(r"done on ([0-9\-:\. ]+)", desc)
            if m_date:
                build_dt = m_date.group(1)[:10]

            rows.append(
                f"<strong>name:</strong> {name}\n"
                f"version: {code_ver or 'unknown'}\n"
                f"biolink: {biolink or 'unknown'}\n"
                f"build date: {build_dt or 'unknown'}"
            )
        return "\n\n".join(rows) if rows else None
    except Exception:
        return None


def summarize_response(r: httpx.Response) -> str:
    """Readable body of a failed response for the failure log. TRAPI responses
    are cut down to their status, description, result count and WARNING/ERROR
    logs (the full knowledge graph isn't useful here)."""
    try:
        data = r.json()
    except Exception:
        return (r.text or "")[:RESPONSE_SNIPPET_MAX]
    if isinstance(data, dict) and ("message" in data or "logs" in data):
        msg = data.get("message")
        logs = [
            entry for entry in (data.get("logs") or [])
            if isinstance(entry, dict) and str(entry.get("level", "")).upper() in ("ERROR", "WARNING")
        ]
        data = {
            "status": data.get("status"),
            "description": data.get("description"),
            "results": len(msg.get("results") or []) if isinstance(msg, dict) else None,
            "logs": logs[:25],
        }
    return json.dumps(data, indent=2, default=str)[:RESPONSE_SNIPPET_MAX]


async def probe_arax_query(url: str, kind: str):
    """Fire the canned TRAPI query of `kind` at an ARAX node. Returns
    (latency_ms, error, http_status, response): latency is None if no 200 came
    back; error is None when the query returned a 200 with at least one result,
    else a short reason, in which case `response` holds the body for the
    failure log."""
    # `submitter` identifies these probe queries in ARAX's logs so they can be
    # told apart from real user traffic.
    body = {"submitter": "UpTimeARAX", "message": {"query_graph": QUERY_GRAPHS[kind]}}
    start = time.perf_counter()
    try:
        r = await http_client.post(
            arax_api(url) + "/query",
            json=body,
            timeout=QUERY_TIMEOUTS[kind],
        )
    except Exception as ex:
        return None, f"query failed: {type(ex).__name__}", None, repr(ex)
    elapsed = int((time.perf_counter() - start) * 1000)
    try:
        data = r.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if r.status_code != 200:
        desc = str(data.get("description") or data.get("detail") or "").strip()
        error = f"HTTP {r.status_code}" + (f": {desc[:200]}" if desc else "")
        return None, error, r.status_code, summarize_response(r)
    results = (data.get("message") or {}).get("results")
    if not results:
        return elapsed, "query returned no results", r.status_code, summarize_response(r)
    return elapsed, None, r.status_code, None


def compute_health(is_up, url: str, query_rows) -> tuple[str | None, list[str]]:
    """(health, failing_kinds) for a monitor. health is None before the first
    check, else "down" (red), "degraded" (yellow) or "up" (green)."""
    kinds = query_kinds_for(url)
    ok_by_kind = {r.kind: r.ok for r in query_rows}
    failing = [k for k in kinds if ok_by_kind.get(k) is False]
    if is_up is None:
        return None, failing
    if not is_up:
        return "down", failing
    if url in FULL_PROBE_ENDPOINTS and failing and len(failing) == len(kinds):
        return "down", failing
    return ("degraded" if failing else "up"), failing


async def apply_health(session, m: Monitor):
    """Recompute m's health from its liveness and query rows; on a change,
    record a StateEvent. Alerting is separate (maybe_alert) so it can wait for
    a recheck to finish."""
    rows = (await session.execute(
        select(QueryStatus).where(QueryStatus.monitor_id == m.id)
    )).scalars().all()
    new, _ = compute_health(m.is_up, m.url, rows)
    if new == m.health:
        return
    m.health = new
    m.last_state_change_ts = int(time.time())
    session.add(StateEvent(
        monitor_id=m.id,
        is_up=new != "down",
        status=new,
        changed_at_ts=m.last_state_change_ts,
    ))


def _join_labels(kinds) -> str:
    """'xDTD', 'xDTD and Pathfinder', 'Lookup, xDTD and Pathfinder'."""
    names = [QUERY_LABELS[k] for k in QUERY_LABELS if k in kinds]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


async def maybe_alert(monitor_id: int):
    """Send one Slack message when a monitor's health, or the set of queries
    failing on a degraded monitor, differs from what was last announced:
        DOWN: <cause>                      (-> red)
        UP                                 (red -> green)
        UP but DEGRADED: <failing>         (red -> yellow)
        DEGRADED: <failing>                (green -> yellow)
        DEGRADED: X restored · now also failing: Y · still failing: Z
                                           (yellow, failing queries changed)
        RESTORED: X is returning successfully again   (yellow -> green)
    Held while a background recheck is running, so a recheck in which several
    queries change produces a single message once all of them are in."""
    if monitor_id in _extended_inflight:
        return
    async with _health_locks[monitor_id]:
        async with AsyncSessionLocal() as session:
            m = await session.get(Monitor, monitor_id)
            if not m:
                return
            prev = _alerted_health.get(monitor_id)
            # A degraded node can change which queries fail without changing
            # health, so it's always re-examined; anything else only on change.
            if m.health == prev and m.health != "degraded":
                return
            rows = (await session.execute(
                select(QueryStatus).where(QueryStatus.monitor_id == monitor_id)
            )).scalars().all()
            last_check = (await session.execute(
                select(Check).where(Check.monitor_id == monitor_id).order_by(Check.id.desc()).limit(1)
            )).scalars().first()
            url, health, is_up = m.url, m.health, m.is_up

        _, failing_list = compute_health(is_up, url, rows)
        failing = set(failing_list)
        prev_failing = _alerted_failing.get(monitor_id)
        _alerted_health[monitor_id] = health
        _alerted_failing[monitor_id] = failing

    if prev is None or health is None:
        return  # first state after a monitor is added isn't news
    if health == prev and (prev_failing is None or failing == prev_failing):
        return  # nothing changed (or re-learning the failing set after a restart)

    errors = {r.kind: r.error for r in rows}

    def describe(kinds, suffix="") -> str:
        """'Pathfinder<suffix> (HTTP 500: ...); xDTD<suffix> (...)'"""
        return "; ".join(
            f"{QUERY_LABELS[k]}{suffix} ({errors.get(k) or 'no detail'})" for k in QUERY_LABELS if k in kinds
        )

    if health == "up":
        if prev == "degraded":
            what = f"{_join_labels(prev_failing)} {'is' if len(prev_failing) == 1 else 'are'}" \
                if prev_failing else "all queries are"
            text = f"{url} RESTORED: {what} returning successfully again"
        else:
            text = f"{url} UP"
    elif health == "degraded":
        if prev == "degraded" and prev_failing is not None:
            parts = []
            if prev_failing - failing:
                parts.append(f"{_join_labels(prev_failing - failing)} restored")
            if failing - prev_failing:
                parts.append(f"now also failing: {describe(failing - prev_failing)}")
            if failing & prev_failing:
                parts.append(f"still failing: {describe(failing & prev_failing)}")
            text = f"{url} DEGRADED: {' · '.join(parts)}"
        else:
            lead = "UP but DEGRADED" if prev == "down" else "DEGRADED"
            text = f"{url} {lead}: {describe(failing, ' failing')}"
    elif not is_up:
        if last_check and last_check.status_code:
            cause = f"HTTP {last_check.status_code}"
        elif last_check and last_check.error_message:
            cause = last_check.error_message.split("(", 1)[0]  # e.g. ConnectTimeout
        else:
            cause = "no response"
        check = "site check" if url in ROOT_LIVENESS_ENDPOINTS else "status check"
        text = f"{url} DOWN: {check} failing ({cause})"
    else:
        text = f"{url} DOWN: all queries failing — {describe(failing, ' failing')}"
    await send_slack_message(text, url)


async def record_query_result(monitor_id: int, url: str, kind: str, latency_ms: int | None,
                              error: str | None, http_status: int | None, response: str | None):
    """Store a probe result, update the node's health and schedule the next
    probe of this kind. Callers alert via maybe_alert() once their round of
    probes is done."""
    now = int(time.time())
    failed = error is not None
    async with _health_locks[monitor_id]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(QueryStatus).where(QueryStatus.monitor_id == monitor_id, QueryStatus.kind == kind)
            )).scalars().first()
            if row is None:
                row = QueryStatus(monitor_id=monitor_id, kind=kind, consecutive_failures=0)
                session.add(row)
            if failed:
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
                if row.consecutive_failures >= QUERY_FAIL_THRESHOLD:
                    row.ok = False
                session.add(QueryFailure(
                    monitor_id=monitor_id, kind=kind, checked_ts=now, http_status=http_status,
                    latency_ms=latency_ms, error=error, response=response,
                ))
            else:
                row.consecutive_failures = 0
                row.ok = True
            row.error, row.latency_ms, row.checked_ts = error, latency_ms, now

            m = await session.get(Monitor, monitor_id)
            if m:
                await apply_health(session, m)
            await session.commit()

    _next_probe[(monitor_id, kind)] = time.monotonic() + (QUERY_RETRY_INTERVAL if failed else QUERY_INTERVAL)


async def run_extended_probes(monitor_id: int, url: str, kinds: list[str]):
    """xDTD / xCRG / Pathfinder, one after another so the node isn't hit with
    several heavy inferred queries at once."""
    try:
        for kind in kinds:
            result = await probe_arax_query(url, kind)
            await record_query_result(monitor_id, url, kind, *result)
    except Exception as e:
        print(f"[PROBE ERROR] {url}: {type(e).__name__}: {e}")
    finally:
        _extended_inflight.discard(monitor_id)
    await maybe_alert(monitor_id)


def load_query_statuses(rows, url: str, tz, now_ts: int) -> list[dict]:
    """Badge data for a monitor, in QUERY_LABELS order; kinds not yet probed
    come back with ok=None."""
    by_kind = {r.kind: r for r in rows}
    out = []
    for kind in query_kinds_for(url):
        r = by_kind.get(kind)
        out.append({
            "kind": kind,
            "label": QUERY_LABELS[kind],
            "ok": r.ok if r else None,
            "error": r.error if r else None,
            "latency_ms": r.latency_ms if r else None,
            # Failed at least once but not yet confirmed failing.
            "rechecking": bool(r and r.ok is not False and (r.consecutive_failures or 0) > 0),
            "stale": bool(r and now_ts - r.checked_ts > QUERY_STALE_AFTER),
            "checked_str": (
                datetime.fromtimestamp(r.checked_ts, tz=tz).strftime("%b %-d, %-I:%M %p %Z")
                if r else None
            ),
        })
    return out


async def run_check(monitor_id: int, url: str):
    is_arax = url in ARAX_ENDPOINTS
    code = 0
    error_message = None
    new_code_version = None
    response_body = None  # kept only when the check fails
    # Latency sample for this check (ms). None means "no sample this check" — the
    # column is nullable and downstream latency stats skip None rows.
    dur = None

    if url in ROOT_LIVENESS_ENDPOINTS:
        try:
            r = await http_client.get(url, follow_redirects=True, timeout=10.0)
            code = r.status_code
            if code != 200:
                response_body = (r.text or "")[:RESPONSE_SNIPPET_MAX]
        except Exception as ex:
            error_message = repr(ex)
    elif is_arax:
        # Liveness via the ARAX status API: a 200 proves the Flask backend is up
        # (not just the static frontend). The same payload carries the version.
        try:
            r = await http_client.get(
                arax_status_url(url), follow_redirects=True, timeout=10.0
            )
            code = r.status_code
            if code == 200:
                new_code_version = parse_arax_version(r.json())
            else:
                response_body = (r.text or "")[:RESPONSE_SNIPPET_MAX]
        except Exception as ex:
            error_message = repr(ex)
    else:
        # Non-ARAX nodes: plain root GET, timed for latency every check.
        start = time.perf_counter()
        try:
            r = await http_client.get(url, follow_redirects=True, timeout=10.0)
            code = r.status_code
            if code != 200:
                response_body = (r.text or "")[:RESPONSE_SNIPPET_MAX]
        except Exception as ex:
            error_message = repr(ex)
        dur = int((time.perf_counter() - start) * 1000)

    is_success = code == 200

    # Refresh metadata / latency outside the DB session so we never hold a write
    # transaction open across a network call.
    if is_success:
        now_mono = time.monotonic()

        if url in ROOT_LIVENESS_ENDPOINTS:
            if now_mono - _last_code_version_fetch.get(monitor_id, 0.0) >= CODE_VERSION_REFRESH:
                new_code_version = await fetch_arax_version(arax_status_url(url))
                _last_code_version_fetch[monitor_id] = now_mono

        if is_arax:
            kinds = query_kinds_for(url)
            # Coming back from DOWN: re-run every query now, so the node's color
            # reflects its current state rather than results from before the outage.
            if _last_status_up.get(monitor_id) is False:
                for kind in kinds:
                    _next_probe.pop((monitor_id, kind), None)
            due = [k for k in kinds if now_mono >= _next_probe.get((monitor_id, k), 0.0)]
            # Stamp before awaiting so a slow reasoner isn't re-hit every poll;
            # record_query_result sets the real next time once a result is in.
            extended = [k for k in due if k != "lookup"]
            if extended and monitor_id not in _extended_inflight:
                for kind in extended:
                    _next_probe[(monitor_id, kind)] = now_mono + QUERY_INTERVAL
                _extended_inflight.add(monitor_id)
                task = asyncio.create_task(run_extended_probes(monitor_id, url, extended))
                _probe_tasks.add(task)
                task.add_done_callback(_probe_tasks.discard)
            if "lookup" in due:
                _next_probe[(monitor_id, "lookup")] = now_mono + QUERY_INTERVAL
                lookup = await probe_arax_query(url, "lookup")
                dur = lookup[0]
                await record_query_result(monitor_id, url, "lookup", *lookup)
        else:
            # Non-ARAX build metadata via /code_version, throttled per monitor.
            if now_mono - _last_code_version_fetch.get(monitor_id, 0.0) >= CODE_VERSION_REFRESH:
                new_code_version = await fetch_code_version(url)
                _last_code_version_fetch[monitor_id] = now_mono

        # Refresh TLS cert expiry (cached in memory), throttled per monitor.
        if now_mono - _last_cert_fetch.get(monitor_id, 0.0) >= CERT_REFRESH:
            _cert_expiry[monitor_id] = await asyncio.to_thread(_get_cert_expiry_ts, url)
            _last_cert_fetch[monitor_id] = now_mono

    async with _health_locks[monitor_id]:
        async with AsyncSessionLocal() as session:
            m = await session.get(Monitor, monitor_id)
            if not m:
                return

            previous_state = m.is_up

            # count recent consecutive failures
            recent_checks = (
                await session.execute(
                    select(Check)
                    .where(Check.monitor_id == monitor_id)
                    .order_by(Check.id.desc())
                    .limit(FAIL_THRESHOLD - 1)
                )
            ).scalars().all()

            consecutive_failures = 0
            if not is_success:
                consecutive_failures = 1
                for c in recent_checks:
                    if c.status_code != 200:
                        consecutive_failures += 1
                    else:
                        break

            confirmed_up = is_success
            confirmed_down = (not is_success) and consecutive_failures >= FAIL_THRESHOLD

            if new_code_version is not None:
                m.code_version = new_code_version

            if previous_state is None:
                m.is_up = confirmed_up  # first check
            elif previous_state and confirmed_down:
                m.is_up = False  # DOWN only after FAIL_THRESHOLD failures
            elif not previous_state and confirmed_up:
                m.is_up = True  # back UP immediately on success

            # Health combines liveness with the query checks; it owns the state
            # events and last_state_change_ts.
            await apply_health(session, m)

            session.add(Check(
                monitor_id=monitor_id,
                status_code=code,
                response_time_ms=dur,
                error_message=error_message,
                code_version=m.code_version,
                response_body=response_body,
            ))

            await session.commit()
            _last_status_up[monitor_id] = m.is_up

    # Held (and sent by run_extended_probes instead) if a recheck is still running.
    await maybe_alert(monitor_id)