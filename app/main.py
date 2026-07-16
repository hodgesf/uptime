import asyncio
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from .database import Base, engine, AsyncSessionLocal
from .models import Monitor, Check, StateEvent

# --- Configuration ---
CHECK_INTERVAL = 30
ENDPOINTS = [
    "https://kg2cploverdb.ci.transltr.io",
    "https://kg2cploverdb.test.transltr.io",
    "https://multiomics.rtx.ai:9990",
    "https://multiomics.ci.transltr.io",
    "https://arax.ncats.io",
    "https://arax.ncats.io/test",
    "https://arax.ncats.io/beta",
    "https://arax.ci.transltr.io",
]

# ARAX endpoints report their build via the ARAX status API rather than a
# /code_version route. Map each monitored URL to its site_config URL (note CI
# needs /test inserted). The reported version is the `tier0-YYYYMMDD` token from
# curie_to_pmids_version.
ARAX_STATUS_URLS = {
    "https://arax.ncats.io": "https://arax.ncats.io/api/arax/v1.4/status?mode=site_config",
    "https://arax.ncats.io/test": "https://arax.ncats.io/test/api/arax/v1.4/status?mode=site_config",
    "https://arax.ncats.io/beta": "https://arax.ncats.io/beta/api/arax/v1.4/status?mode=site_config",
    "https://arax.ci.transltr.io": "https://arax.ci.transltr.io/test/api/arax/v1.4/status?mode=site_config",
}

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Build-metadata refresh throttle: re-fetch {url}/code_version at most this often
# per monitor, and never while holding a DB session open.
CODE_VERSION_REFRESH = 300  # seconds
_last_code_version_fetch: dict[int, float] = {}

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

async def send_slack_message(text: str):
    if not SLACK_WEBHOOK:
        return
    try:
        await http_client.post(SLACK_WEBHOOK, json={"text": text})
    except Exception:
        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
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

        # Delete removed
        for m in monitors:
            if m.url not in ENDPOINTS:
                await session.delete(m)

        await session.commit()

    checker_task = asyncio.create_task(checker_loop())
    yield
    checker_task.cancel()
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


def compute_daily_status(events, now_ts, tz, days=30):
    """Reconstruct per-day up/down status for the last `days` days from state
    events. Returns a list (oldest first) of dicts:
        {date, status: up|down|degraded|nodata, uptime: float|None,
         down_seconds, up_seconds, total_seconds}
    """
    evs = sorted(events, key=lambda e: e.changed_at_ts)
    monitoring_start = evs[0].changed_at_ts if evs else None

    # Contiguous state intervals [start, end) with a boolean up flag; the last
    # event runs to now.
    intervals = []
    for i, e in enumerate(evs):
        start = e.changed_at_ts
        end = evs[i + 1].changed_at_ts if i + 1 < len(evs) else now_ts
        if end > start:
            intervals.append((start, end, e.is_up))

    now_local = datetime.fromtimestamp(now_ts, tz)
    today_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    result = []
    for d in range(days - 1, -1, -1):
        day_start_local = today_midnight - timedelta(days=d)
        day_end_local = day_start_local + timedelta(days=1)
        day_start = int(day_start_local.timestamp())
        day_end = int(day_end_local.timestamp())
        label = day_start_local.strftime("%b %-d")

        eff_end = min(day_end, now_ts)
        eff_start = day_start if monitoring_start is None else max(day_start, monitoring_start)

        if monitoring_start is None or eff_end <= eff_start:
            result.append({"date": label, "status": "nodata", "uptime": None,
                           "down_seconds": 0, "up_seconds": 0, "total_seconds": 0})
            continue

        total = eff_end - eff_start
        down = 0
        for s, e_, up in intervals:
            if up:
                continue
            overlap = min(e_, eff_end) - max(s, eff_start)
            if overlap > 0:
                down += overlap
        up_sec = total - down
        if down == 0:
            status = "up"
        elif up_sec <= 0:
            status = "down"
        else:
            status = "degraded"
        result.append({"date": label, "status": status,
                       "uptime": round(up_sec / total * 100, 3),
                       "down_seconds": int(down), "up_seconds": int(up_sec),
                       "total_seconds": int(total)})
    return result


def overall_uptime(daily):
    """Time-weighted uptime percent across the daily buckets, or None."""
    total = sum(d["total_seconds"] for d in daily)
    up = sum(d["up_seconds"] for d in daily)
    return round(up / total * 100, 3) if total > 0 else None

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/monitor/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail(request: Request, monitor_id: int):
    pacific = ZoneInfo("America/Los_Angeles")
    now_ts = int(time.time())
    one_day_ago_dt = datetime.now(ZoneInfo("UTC")) - timedelta(hours=24)
    one_day_ago_ts = int(one_day_ago_dt.timestamp())

    async with AsyncSessionLocal() as session:
        monitor = await session.get(Monitor, monitor_id)
        if not monitor: raise HTTPException(status_code=404)
        
        events = (await session.execute(
            select(StateEvent)
            .where(StateEvent.monitor_id == monitor_id, StateEvent.changed_at_ts >= one_day_ago_ts)
            .order_by(StateEvent.changed_at_ts.desc())
        )).scalars().all()
        
        checks = (await session.execute(
            select(Check)
            .where(Check.monitor_id == monitor_id, Check.checked_at >= one_day_ago_dt)
            .order_by(Check.checked_at.asc())
        )).scalars().all()

        all_events = (await session.execute(
            select(StateEvent)
            .where(StateEvent.monitor_id == monitor_id)
            .order_by(StateEvent.changed_at_ts.asc())
        )).scalars().all()

    # 30-day daily uptime for the status-bar strip
    daily = compute_daily_status(all_events, now_ts, pacific, days=30)
    uptime_30d = overall_uptime(daily)

    # Prep Stats - only calculate if first check has completed
    if monitor.last_state_change_ts is not None:
        chart_data = [c.response_time_ms for c in checks]
        # Ensure checked_at is treated as UTC before converting to Pacific
        chart_labels = []
        for c in checks:
            # If datetime is naive, assume it's UTC; if it has tzinfo, use as-is
            dt = c.checked_at if c.checked_at.tzinfo else c.checked_at.replace(tzinfo=ZoneInfo("UTC"))
            chart_labels.append(dt.astimezone(pacific).strftime('%I:%M %p %Z'))
        avg_lat = round(sum(chart_data) / len(chart_data), 2) if chart_data else 0
        up_checks = [c for c in checks if c.status_code == 200]
        uptime_pct = round((len(up_checks) / len(checks)) * 100, 2) if checks else 0
        time_in_status_sec = now_ts - monitor.last_state_change_ts
        time_in_status_str = format_duration_str(time_in_status_sec)
    else:
        chart_data = []
        chart_labels = []
        avg_lat = 0
        uptime_pct = 0
        time_in_status_str = "Pending"

    if monitor.is_up is None:
        status_label, status_class = "INITIALIZING...", "pending"
    else:
        status_label, status_class = ("UP" if monitor.is_up else "DOWN"), ("up" if monitor.is_up else "down")

    # Capture the current status now, before the downtime loop below reuses the
    # `status_label` name for its per-check labels.
    current_status_label, current_status_class = status_label, status_class

    # FIX: Ensure Pacific conversion for Raw Logs - only if initialized
    raw_logs_list = []
    if monitor.last_state_change_ts is not None:
        for c in reversed(checks):
            # If datetime is naive, assume it's UTC; if it has tzinfo, use as-is
            dt = c.checked_at if c.checked_at.tzinfo else c.checked_at.replace(tzinfo=ZoneInfo("UTC"))
            raw_logs_list.append(f"<tr><td>{dt.astimezone(pacific).strftime('%m/%d %I:%M:%S %p %Z')}</td><td>{c.status_code}</td></tr>")
    raw_logs = "".join(raw_logs_list)

    # Build downtime analysis
    downtime_sections = []
    sorted_events = sorted(events, key=lambda e: e.changed_at_ts, reverse=True)
    
    for i, event in enumerate(sorted_events):
        # Find DOWN events (events where is_up changed to False)
        if not event.is_up:
            # Find the recovery event (next event where is_up is True)
            recovery_event = None
            for j in range(i-1, -1, -1):
                if sorted_events[j].is_up:
                    recovery_event = sorted_events[j]
                    break
            
            if recovery_event:
                down_start_ts = event.changed_at_ts
                recovery_ts = recovery_event.changed_at_ts
                
                # Get checks: 2 before down, during down, 2 after recovery
                downtime_checks = [c for c in checks if c.checked_at >= datetime.fromtimestamp(down_start_ts - 300, tz=ZoneInfo("UTC")).replace(tzinfo=None) and c.checked_at <= datetime.fromtimestamp(recovery_ts + 300, tz=ZoneInfo("UTC")).replace(tzinfo=None)]
                
                if downtime_checks:
                    downtime_html = '<table style="width:100%; border-collapse: collapse;"><thead><tr><th style="text-align:left; padding:8px; border-bottom:1px solid var(--border-color);">Time (PT)</th><th style="text-align:left; padding:8px; border-bottom:1px solid var(--border-color);">Status Code</th><th style="text-align:left; padding:8px; border-bottom:1px solid var(--border-color);">Status</th><th style="text-align:left; padding:8px; border-bottom:1px solid var(--border-color);">Code Version</th></tr></thead><tbody>'
                    
                    # Track code versions before and after for change detection
                    before_down_checks = []
                    after_recovery_checks = []
                    
                    for c in downtime_checks:
                        dt = c.checked_at if c.checked_at.tzinfo else c.checked_at.replace(tzinfo=ZoneInfo("UTC"))
                        check_time = dt.astimezone(pacific).strftime('%m/%d %I:%M:%S %p %Z')
                        
                        # Get timestamp consistently - convert to UTC datetime if needed, then get timestamp
                        if c.checked_at.tzinfo:
                            check_ts = int(c.checked_at.timestamp())
                        else:
                            # Naive datetime - assume UTC and convert
                            check_ts = int(c.checked_at.replace(tzinfo=ZoneInfo("UTC")).timestamp())
                        
                        # Determine status: before down, during down, or after recovery
                        if check_ts < down_start_ts:
                            status_label = "Before Down"
                            before_down_checks.append(c)
                        elif check_ts >= recovery_ts:
                            status_label = "After Recovery"
                            after_recovery_checks.append(c)
                        else:
                            status_label = "During Down"
                        
                        status_color = "var(--down-color)" if status_label == "During Down" else "var(--text-secondary)"
                        code_color = "var(--down-color)" if c.status_code == 0 or c.status_code >= 400 else "var(--up-color)"
                        
                        # Display error message if status code is 0, otherwise show the code
                        error_display = c.error_message if c.status_code == 0 and c.error_message else str(c.status_code)
                        
                        # Extract build date from check's code_version
                        build_date = "Unknown"
                        if c.code_version:
                            # Try multiple patterns for extraction
                            date_match = re.search(r"done on\s+(\d{4}-\d{2}-\d{2})", c.code_version)
                            if date_match:
                                build_date = date_match.group(1)
                            else:
                                date_match = re.search(r"build date:\s*(\d{4}-\d{2}-\d{2})", c.code_version)
                                if date_match:
                                    build_date = date_match.group(1)
                                else:
                                    any_date_match = re.search(r"(\d{4}-\d{2}-\d{2})", c.code_version)
                                    if any_date_match:
                                        build_date = any_date_match.group(1)
                        
                        downtime_html += f'<tr><td style="padding:8px; border-bottom:1px solid var(--border-color);">{check_time}</td><td style="padding:8px; border-bottom:1px solid var(--border-color); color:{code_color}; font-weight:bold;">{error_display}</td><td style="padding:8px; border-bottom:1px solid var(--border-color); color:{status_color}; font-weight:500;">{status_label}</td><td style="padding:8px; border-bottom:1px solid var(--border-color); font-size:0.9em; color:var(--text-secondary);">{build_date}</td></tr>'
                    
                    downtime_html += '</tbody></table>'
                    
                    down_start_str = datetime.fromtimestamp(down_start_ts, tz=pacific).strftime('%m/%d %I:%M %p %Z')
                    recovery_str = datetime.fromtimestamp(recovery_ts, tz=pacific).strftime('%m/%d %I:%M %p %Z')
                    downtime_duration = format_duration_str(recovery_ts - down_start_ts)
                    
                    # Check if code version changed between before and after recovery
                    code_version_changed = False
                    change_note = ""
                    if before_down_checks and after_recovery_checks:
                        # Get the last check before down and first check after recovery
                        last_before = before_down_checks[-1]
                        first_after = after_recovery_checks[0]
                        
                        # Extract build dates from both
                        def extract_build_date(check):
                            if not check.code_version:
                                return None
                            # Try multiple patterns
                            # Pattern 1: "done on YYYY-MM-DD"
                            date_match = re.search(r"done on\s+(\d{4}-\d{2}-\d{2})", check.code_version)
                            if date_match:
                                return date_match.group(1)
                            # Pattern 2: "build date: YYYY-MM-DD"
                            date_match = re.search(r"build date:\s*(\d{4}-\d{2}-\d{2})", check.code_version)
                            if date_match:
                                return date_match.group(1)
                            # Pattern 3: Any YYYY-MM-DD pattern
                            any_date_match = re.search(r"(\d{4}-\d{2}-\d{2})", check.code_version)
                            if any_date_match:
                                return any_date_match.group(1)
                            return None
                        
                        before_date = extract_build_date(last_before)
                        after_date = extract_build_date(first_after)
                        
                        if before_date and after_date and before_date != after_date:
                            code_version_changed = True
                            change_note = f' <span style="color: var(--link-color); font-weight: 500;">⚠️ Code version changed: {before_date} → {after_date}</span>'
                    
                    downtime_sections.append(f'''
                    <details style="margin-bottom: 15px; border: 1px solid var(--border-color); border-radius: 8px; padding: 10px; background: var(--card-bg);">
                        <summary style="font-weight: 600; cursor: pointer; padding: 5px; color: var(--down-color);">Downtime Event - {down_start_str} ({downtime_duration}){change_note}</summary>
                        <div style="padding:15px; margin-top:10px;">
                            {downtime_html}
                        </div>
                    </details>
                    ''')
    
    downtime_section_html = "".join(downtime_sections) if downtime_sections else ""

    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "monitor_url": monitor.url,
            "status_label": current_status_label,
            "status_class": current_status_class,
            "initialized": monitor.last_state_change_ts is not None,
            "time_in_status_str": time_in_status_str,
            "avg_lat": avg_lat,
            "uptime_pct": uptime_pct,
            "daily": daily,
            "uptime_30d": uptime_30d,
            "downtime_html": downtime_section_html,
            "raw_logs_html": raw_logs,
            "chart_labels": chart_labels,
            "chart_data": chart_data,
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

    events_by_monitor: dict[int, list] = {}
    for e in all_events:
        events_by_monitor.setdefault(e.monitor_id, []).append(e)

    result = []
    for m in monitors:
        if m.last_state_change_ts is not None:
            change_str = datetime.fromtimestamp(m.last_state_change_ts, tz=pacific).strftime("%b %-d, %-I:%M %p %Z")
        else:
            change_str = "Pending"
        daily = compute_daily_status(events_by_monitor.get(m.id, []), now_ts, pacific, days=30)
        result.append({
            "id": m.id,
            "url": m.url,
            "is_up": m.is_up,
            "last_state_change_ts": m.last_state_change_ts or 0,
            "last_state_change_str": change_str,
            "code_version": m.code_version,
            "uptime_30d": overall_uptime(daily),
            "daily": [{"date": d["date"], "status": d["status"], "uptime": d["uptime"]} for d in daily],
        })
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

    # Calculate stats
    if monitor.last_state_change_ts is not None:
        time_in_status_sec = now_ts - monitor.last_state_change_ts
        time_in_status_str = format_duration_str(time_in_status_sec)
        
        chart_data = [c.response_time_ms for c in checks]
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
            "status": "UP" if e.is_up else "DOWN"
        })

    return {
        "id": monitor.id,
        "url": monitor.url,
        "is_up": monitor.is_up,
        "last_state_change_ts": monitor.last_state_change_ts or 0,
        "last_state_change_str": change_str,
        "code_version": monitor.code_version,
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
                targets = [(m.id, m.url) for m in monitors]
            print(f"[CHECKER] Running checks for {len(targets)} monitors...")
            await asyncio.gather(*[run_check(mid, url) for mid, url in targets])
            print(f"[CHECKER] Checks completed")
        except Exception as e:
            print(f"[CHECKER ERROR] {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(CHECK_INTERVAL)

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
    if url in ARAX_STATUS_URLS:
        return await fetch_arax_version(ARAX_STATUS_URLS[url])
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


async def run_check(monitor_id: int, url: str):
    start = time.perf_counter()
    code = 0
    error_message = None

    try:
        r = await http_client.get(url, follow_redirects=True, timeout=10.0)
        code = r.status_code
    except Exception as ex:
        error_message = repr(ex)

    dur = int((time.perf_counter() - start) * 1000)
    is_success = code == 200

    # Refresh build metadata outside the DB session, and at most once per
    # CODE_VERSION_REFRESH seconds per monitor, so we never hold a write
    # transaction open across a network call or re-fetch identical data.
    new_code_version = None
    if is_success:
        now_mono = time.monotonic()
        if now_mono - _last_code_version_fetch.get(monitor_id, 0.0) >= CODE_VERSION_REFRESH:
            new_code_version = await fetch_code_version(url)
            _last_code_version_fetch[monitor_id] = now_mono

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

        # first check
        if previous_state is None:
            m.is_up = confirmed_up
            m.last_state_change_ts = int(time.time())
            session.add(StateEvent(
                monitor_id=monitor_id,
                is_up=confirmed_up,
                changed_at_ts=m.last_state_change_ts
            ))

        # transition to DOWN (only after threshold)
        elif previous_state and confirmed_down:
            m.is_up = False
            m.last_state_change_ts = int(time.time())
            session.add(StateEvent(
                monitor_id=monitor_id,
                is_up=False,
                changed_at_ts=m.last_state_change_ts
            ))
            await send_slack_message(f"{url} is DOWN")

        # transition to UP immediately on success
        elif not previous_state and confirmed_up:
            m.is_up = True
            m.last_state_change_ts = int(time.time())
            session.add(StateEvent(
                monitor_id=monitor_id,
                is_up=True,
                changed_at_ts=m.last_state_change_ts
            ))
            await send_slack_message(f"{url} is BACK UP")

        session.add(Check(
            monitor_id=monitor_id,
            status_code=code,
            response_time_ms=dur,
            error_message=error_message,
            code_version=m.code_version
        ))

        await session.commit()