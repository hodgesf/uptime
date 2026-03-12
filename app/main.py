import asyncio
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
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
]

http_client = httpx.AsyncClient(timeout=10, verify=False)

import os

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")

import re
from urllib.parse import urlparse

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
    if seconds < 0: seconds = 0
    days = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    if s > 0 or not parts:  # Always show seconds if nothing else
        parts.append(f"{s}s")
    
    return " ".join(parts)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <html>
    <head>
        <title>Endpoint Monitor</title>
        <style>
            :root {
                --bg-primary: #ffffff;
                --bg-secondary: #f8f9fa;
                --text-primary: #1a1a1a;
                --text-secondary: #666666;
                --border-color: #e0e0e0;
                --header-bg: #2c3e50;
                --header-text: #ffffff;
                --table-hover: #f5f5f5;
                --up-color: #22c55e;
                --down-color: #ef4444;
                --pending-color: #9ca3af;
                --link-color: #0066cc;
                --code-bg: #f5f5f5;
                --code-border: #d0d0d0;
            }
            
            [data-theme="dark"] {
                --bg-primary: #1e1e1e;
                --bg-secondary: #2d2d2d;
                --text-primary: #ffffff;
                --text-secondary: #b0b0b0;
                --border-color: #404040;
                --header-bg: #1a2332;
                --header-text: #ffffff;
                --table-hover: #2d2d2d;
                --up-color: #22c55e;
                --down-color: #ff5252;
                --pending-color: #9ca3af;
                --link-color: #4da6ff;
                --code-bg: #2d2d2d;
                --code-border: #404040;
            }
            
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                margin: 0;
                padding: 20px 40px;
                background: var(--bg-primary);
                color: var(--text-primary);
            }
            
            .header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 30px;
            }
            
            h2 { 
                margin: 0;
                font-size: 28px;
                font-weight: 600;
            }
            
            .theme-toggle {
                background: var(--header-bg);
                color: var(--header-text);
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
                transition: opacity 0.2s;
            }
            
            .theme-toggle:hover {
                opacity: 0.8;
            }
            
            table { 
                width: 100%; 
                border-collapse: collapse; 
                background: var(--bg-primary); 
                border-radius: 8px; 
                overflow: hidden; 
                box-shadow: 0 2px 8px rgba(0,0,0,0.1); 
            }
            
            th, td { 
                padding: 16px; 
                text-align: left; 
                border-bottom: 1px solid var(--border-color); 
            }
            
            th { 
                background-color: var(--header-bg); 
                color: var(--header-text);
                font-weight: 600;
                font-size: 13px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            tr:hover { 
                background-color: var(--table-hover); 
            }
            
            .up { 
                color: var(--up-color); 
                font-weight: 700;
                font-size: 14px;
            }
            
            .down { 
                color: var(--down-color); 
                font-weight: 700;
                font-size: 14px;
            }
            
            .pending { 
                color: var(--pending-color); 
                font-weight: 600; 
                font-style: italic; 
                font-size: 14px;
            }
            
            a { 
                color: var(--link-color); 
                text-decoration: none; 
                font-weight: 500; 
            }
            
            a:hover { 
                text-decoration: underline;
            }
            
            .endpoint-link {
                color: var(--link-color);
                font-size: 0.9em;
                font-weight: 500;
            }
            
            .code-version-container {
                display: flex;
                flex-direction: column;
            }
            
            .code-version-summary {
                font-weight: 500;
                color: var(--text-primary);
                font-size: 14px;
            }
            
            .code-version-link {
                color: var(--link-color);
                font-size: 0.8em;
                text-decoration: none;
                cursor: pointer;
                margin-top: 4px;
                display: inline-block;
                font-weight: 500;
            }
            
            .code-version-link:hover {
                text-decoration: underline;
            }
            
            .code-version-details {
                max-width: 450px;
                max-height: 0;
                overflow: hidden;
                white-space: pre-wrap;
                font-size: 13px;
                padding: 0px 8px;
                border: none;
                background: var(--code-bg);
                margin-top: 0px;
                border-radius: 4px;
                transition: max-height 0.3s ease, padding 0.3s ease, border 0.3s ease, margin-top 0.3s ease;
                color: var(--text-primary);
            }
            
            .code-version-details.expanded {
                max-height: 120px;
                overflow-y: auto;
                padding: 8px;
                border: 1px solid var(--code-border);
                margin-top: 8px;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h2>System Status</h2>
            <button class="theme-toggle" onclick="toggleTheme()">🌙 Dark Mode</button>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Endpoint</th>
                    <th>Status</th>
                    <th>Status Since</th>
                    <th>Duration</th>
                    <th>Code Version</th>
                    <th></th>
                </tr>
            </thead>
            <tbody id="monitor-body"></tbody>
        </table>
        <script>
            let rowRefs = {};

            async function loadStatus() {
                try {
                    const res = await fetch("/status");
                    const data = await res.json();
                    const tbody = document.getElementById("monitor-body");
                    data.forEach(m => {
                        if (!rowRefs[m.url]) {
                            const row = document.createElement("tr");
                            row.innerHTML = `
                                <td><a href="/monitor/${m.id}">${m.url}</a></td>
                                <td class="status-cell"></td>
                                <td class="status-since"></td>
                                <td class="state-time"></td>
                                <td>
                                    <div class="code-version-container">
                                        <div class="code-version-summary"></div>
                                        <a class="code-version-link">show more</a>
                                        <div class="code-version-details"></div>
                                    </div>
                                </td>
                                <td><a href="${m.url}" target="_blank" rel="noopener noreferrer" class="endpoint-link">visit endpoint</a></td>
                            `;
                            tbody.appendChild(row);
                            rowRefs[m.url] = {
                                statusCell: row.querySelector(".status-cell"),
                                sinceCell: row.querySelector(".status-since"),
                                timeCell: row.querySelector(".state-time"),
                                codeSummary: row.querySelector(".code-version-summary"),
                                codeDetails: row.querySelector(".code-version-details"),
                                codeLink: row.querySelector(".code-version-link"),
                                codeContainer: row.querySelector(".code-version-container"),
                                ts: m.last_state_change_ts
                            };
                        }

                        const ref = rowRefs[m.url];
                        
                        if (m.is_up === null) {
                            ref.statusCell.textContent = 'CHECKING...';
                            ref.statusCell.className = 'pending';
                        } else {
                            ref.statusCell.textContent = m.is_up ? 'UP' : 'DOWN';
                            ref.statusCell.className = m.is_up ? 'up' : 'down';
                        }
                        ref.sinceCell.textContent = m.last_state_change_str;
                        ref.ts = m.last_state_change_ts;
                        
                        // Extract build date from code_version (look for "done on" followed by date)
                        const codeVersionText = m.code_version || "—";
                        let buildDate = "Unknown";
                        const dateMatch = codeVersionText.match(/done on\s+(\d{4}-\d{2}-\d{2})/);
                        if (dateMatch) {
                            buildDate = dateMatch[1];
                        } else {
                            // Fallback: look for any date pattern in YYYY-MM-DD format
                            const anyDateMatch = codeVersionText.match(/(\d{4}-\d{2}-\d{2})/);
                            if (anyDateMatch) {
                                buildDate = anyDateMatch[1];
                            }
                        }
                        
                        ref.codeSummary.textContent = buildDate;
                        ref.codeDetails.innerHTML = codeVersionText;
                        
                        // Add click handler for toggle (only once)
                        if (!ref.codeLink.hasClickHandler) {
                            ref.codeLink.addEventListener('click', (e) => {
                                e.preventDefault();
                                ref.codeDetails.classList.toggle('expanded');
                                ref.codeLink.textContent = ref.codeDetails.classList.contains('expanded') ? 'show less' : 'show more';
                            });
                            ref.codeLink.hasClickHandler = true;
                        }
                    });
                } catch (e) { console.error(e); }
            }

            function updateTimers() {
                const now = Math.floor(Date.now() / 1000);
                Object.values(rowRefs).forEach(ref => {
                    if (ref.ts === 0) {
                        ref.timeCell.textContent = 'Pending';
                    } else {
                        const s = Math.max(0, now - ref.ts);
                        const h = Math.floor(s / 3600);
                        
                        if (h >= 24) {
                            const d = Math.floor(h / 24);
                            const remainingH = h % 24;
                            const m = Math.floor((s % 3600) / 60);
                            const sec = s % 60;
                            ref.timeCell.textContent = `${d}d ${remainingH}h ${m}m ${sec}s`;
                        } else {
                            const m = Math.floor((s % 3600) / 60);
                            const sec = s % 60;
                            ref.timeCell.textContent = `${h}h ${m}m ${sec}s`;
                        }
                    }
                });
            }

            loadStatus();
            setInterval(loadStatus, 5000);
            setInterval(updateTimers, 1000);
            
            // Dark mode toggle
            function toggleTheme() {
                const html = document.documentElement;
                const isDark = html.getAttribute('data-theme') === 'dark';
                const newTheme = isDark ? 'light' : 'dark';
                html.setAttribute('data-theme', newTheme);
                localStorage.setItem('theme', newTheme);
                updateThemeButton();
            }
            
            function updateThemeButton() {
                const button = document.querySelector('.theme-toggle');
                const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
                button.textContent = isDark ? '☀️ Light Mode' : '🌙 Dark Mode';
            }
            
            // Initialize theme from localStorage or prefer dark mode
            const savedTheme = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
            document.documentElement.setAttribute('data-theme', savedTheme);
            updateThemeButton();
            
        </script>
    </body>
    </html>
    """

@app.get("/monitor/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail(monitor_id: int):
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
        up_checks = [c for c in checks if 0 < c.status_code < 400]
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

    # --- Fixed Timeline Formatting ---
    timeline_rows = []
    
    # Only show current state if it actually exists (state change has occurred)
    if monitor.last_state_change_ts is not None and monitor.is_up is not None:
        current_start_dt = datetime.fromtimestamp(monitor.last_state_change_ts, tz=pacific).strftime('%m/%d %I:%M:%S %p %Z')
        timeline_rows.append(f"""
            <tr>
                <td class='{status_class}'>{status_label} (Current)</td>
                <td>{current_start_dt}</td>
                <td id="live-duration">{time_in_status_str}</td>
            </tr>
        """)
        
        next_ts = monitor.last_state_change_ts
        # Show all state events in reverse chronological order
        for e in events:
            if e.changed_at_ts == monitor.last_state_change_ts:
                continue  # Skip duplicate of current state
                
            duration_sec = next_ts - e.changed_at_ts
            s_class = "up" if e.is_up else "down"
            label = "UP" if e.is_up else "DOWN"
            start_time = datetime.fromtimestamp(e.changed_at_ts, tz=pacific).strftime('%m/%d %I:%M:%S %p %Z')
            
            timeline_rows.append(f"<tr><td class='{s_class}'>{label}</td><td>{start_time}</td><td>{format_duration_str(duration_sec)}</td></tr>")
            next_ts = e.changed_at_ts
    else:
        # No state change has occurred yet - still initializing
        timeline_rows = [f"<tr><td class='pending'>INITIALIZING</td><td>Awaiting first check...</td><td id=\"live-duration\">Pending</td></tr>"]

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
                        
                        print(f"[DEBUG] Check: ts={check_ts}, down_start={down_start_ts}, recovery={recovery_ts}")
                        
                        # Determine status: before down, during down, or after recovery
                        if check_ts < down_start_ts:
                            status_label = "Before Down"
                            before_down_checks.append(c)
                            print(f"  -> Before Down")
                        elif check_ts >= recovery_ts:
                            status_label = "After Recovery"
                            after_recovery_checks.append(c)
                            print(f"  -> After Recovery")
                        else:
                            status_label = "During Down"
                            print(f"  -> During Down")
                        
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
                        
                        # Debug logging
                        print(f"[DEBUG] Downtime event detected:")
                        print(f"  before_down_checks: {len(before_down_checks)} checks")
                        print(f"  after_recovery_checks: {len(after_recovery_checks)} checks")
                        print(f"  last_before.code_version: {last_before.code_version[:50] if last_before.code_version else 'None'}...")
                        print(f"  first_after.code_version: {first_after.code_version[:50] if first_after.code_version else 'None'}...")
                        print(f"  before_date: {before_date}")
                        print(f"  after_date: {after_date}")
                        
                        if before_date and after_date and before_date != after_date:
                            code_version_changed = True
                            change_note = f' <span style="color: var(--link-color); font-weight: 500;">⚠️ Code version changed: {before_date} → {after_date}</span>'
                            print(f"  ✅ CODE VERSION CHANGE DETECTED: {before_date} → {after_date}")
                    
                    downtime_sections.append(f'''
                    <details style="margin-bottom: 15px; border: 1px solid var(--border-color); border-radius: 8px; padding: 10px; background: var(--card-bg);">
                        <summary style="font-weight: 600; cursor: pointer; padding: 5px; color: var(--down-color);">Downtime Event - {down_start_str} ({downtime_duration}){change_note}</summary>
                        <div style="padding:15px; margin-top:10px;">
                            {downtime_html}
                        </div>
                    </details>
                    ''')
    
    downtime_section_html = "".join(downtime_sections) if downtime_sections else ""

    return f"""
    <html>
    <head>
        <title>{monitor.url}</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root {{
                --bg-primary: #ffffff;
                --bg-secondary: #f8f9fa;
                --text-primary: #1a1a1a;
                --text-secondary: #666666;
                --border-color: #e0e0e0;
                --header-bg: #2c3e50;
                --header-text: #ffffff;
                --table-hover: #f5f5f5;
                --up-color: #22c55e;
                --down-color: #ef4444;
                --pending-color: #9ca3af;
                --link-color: #0066cc;
                --code-bg: #f5f5f5;
                --code-border: #d0d0d0;
                --card-bg: #f8f9fa;
                --card-border: #6366f1;
            }}
            
            [data-theme="dark"] {{
                --bg-primary: #1e1e1e;
                --bg-secondary: #2d2d2d;
                --text-primary: #ffffff;
                --text-secondary: #b0b0b0;
                --border-color: #404040;
                --header-bg: #1a2332;
                --header-text: #ffffff;
                --table-hover: #2d2d2d;
                --up-color: #22c55e;
                --down-color: #ff5252;
                --pending-color: #9ca3af;
                --link-color: #4da6ff;
                --code-bg: #2d2d2d;
                --code-border: #404040;
                --card-bg: #2d2d2d;
                --card-border: #404040;
            }}
            
            body {{ 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                margin: 0;
                padding: 20px 40px;
                background: var(--bg-primary);
                color: var(--text-primary);
            }}
            
            .header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 30px;
            }}
            
            h2 {{ 
                margin: 0;
                font-size: 28px;
                font-weight: 600;
            }}
            
            .theme-toggle {{
                background: var(--header-bg);
                color: var(--header-text);
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
                transition: opacity 0.2s;
            }}
            
            .theme-toggle:hover {{
                opacity: 0.8;
            }}
            
            .container {{ 
                max-width: 1000px; 
                margin: auto; 
                background: var(--bg-primary); 
                padding: 30px; 
                border-radius: 12px; 
                box-shadow: 0 2px 8px rgba(0,0,0,0.1); 
            }}
            
            .back-link {{
                text-decoration: none;
                color: var(--link-color);
                font-weight: 500;
            }}
            
            .back-link:hover {{
                text-decoration: underline;
            }}
            
            .stats-grid {{ 
                display: grid; 
                grid-template-columns: repeat(4, 1fr); 
                gap: 15px; 
                margin-bottom: 25px; 
            }}
            
            .stat-card {{ 
                background: var(--card-bg); 
                padding: 15px; 
                border-radius: 8px; 
                border-left: 4px solid var(--card-border);
            }}
            
            .stat-card h4 {{ 
                margin: 0; 
                font-size: 0.75em; 
                color: var(--text-secondary); 
                text-transform: uppercase;
                letter-spacing: 0.5px;
                font-weight: 600;
            }}
            
            .stat-card p {{ 
                margin: 8px 0 0; 
                font-size: 1.2em; 
                font-weight: 600;
                color: var(--text-primary);
            }}
            
            table {{ 
                width: 100%; 
                border-collapse: collapse;
                background: var(--bg-primary);
            }}
            
            th, td {{ 
                padding: 12px; 
                text-align: left; 
                border-bottom: 1px solid var(--border-color);
            }}
            
            th {{
                background-color: var(--header-bg);
                color: var(--header-text);
                font-weight: 600;
                font-size: 13px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            
            tr:hover {{
                background-color: var(--table-hover);
            }}
            
            .up {{ 
                color: var(--up-color); 
                font-weight: 700;
                font-size: 14px;
            }}
            
            .down {{ 
                color: var(--down-color); 
                font-weight: 700;
                font-size: 14px;
            }}
            
            .pending {{ 
                color: var(--pending-color); 
                font-style: italic;
                font-size: 14px;
            }}
            
            details {{ 
                margin-bottom: 15px; 
                border: 1px solid var(--border-color); 
                border-radius: 8px; 
                padding: 10px;
                background: var(--card-bg);
            }}
            
            summary {{ 
                font-weight: 600; 
                cursor: pointer; 
                padding: 5px;
                color: var(--text-primary);
            }}
            
            summary:hover {{
                opacity: 0.8;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2>{monitor.url}</h2>
            <button class="theme-toggle" onclick="toggleTheme()">🌙 Dark Mode</button>
        </div>
        <div class="container">
            <a href="/" class="back-link">← Back to Dashboard</a>
            
            <div class="stats-grid">
                <div class="stat-card"><h4>Current Status</h4><p class='{status_class}'>{status_label}</p></div>
                <div class="stat-card"><h4>Status Since</h4><p id="stat-duration">{'Initializing' if monitor.last_state_change_ts is None else time_in_status_str}</p></div>
                <div class="stat-card"><h4>Avg Latency</h4><p>{'Initializing' if monitor.last_state_change_ts is None else f'{avg_lat}ms'}</p></div>
                <div class="stat-card"><h4>24h Uptime</h4><p>{'Initializing' if monitor.last_state_change_ts is None else f'{uptime_pct}%'}</p></div>
            </div>

            <details>
                <summary>Status Timeline (Last 24h)</summary>
                <table>
                    <thead><tr><th>State</th><th>Started At</th><th>Duration</th></tr></thead>
                    <tbody>{"".join(timeline_rows)}</tbody>
                </table>
            </details>

            {downtime_section_html}

            <details>
                <summary>Latency Graph & Metrics</summary>
                <div style="padding:15px;">
                    {'<p style="color: #9ca3af; font-style: italic;">Initializing - awaiting first check...</p>' if monitor.last_state_change_ts is None else f'<canvas id="latencyChart" height="100"></canvas>'}
                </div>
            </details>

            <details>
                <summary>Raw Request Logs</summary>
                <table>
                    <thead><tr><th>Time (PT)</th><th>Code</th></tr></thead>
                    <tbody>{'<tr><td colspan="2" style="color: #9ca3af; font-style: italic; text-align: center;">Initializing - awaiting first check...</td></tr>' if monitor.last_state_change_ts is None else raw_logs}</tbody>
                </table>
            </details>
        </div>
        <script>
            // Dark mode toggle
            function toggleTheme() {{
                const html = document.documentElement;
                const isDark = html.getAttribute('data-theme') === 'dark';
                const newTheme = isDark ? 'light' : 'dark';
                html.setAttribute('data-theme', newTheme);
                localStorage.setItem('theme', newTheme);
                updateThemeButton();
            }}
            
            function updateThemeButton() {{
                const button = document.querySelector('.theme-toggle');
                const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
                button.textContent = isDark ? '☀️ Light Mode' : '🌙 Dark Mode';
            }}
            
            // Initialize theme from localStorage or prefer dark mode
            const savedTheme = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
            document.documentElement.setAttribute('data-theme', savedTheme);
            updateThemeButton();
            
            const startTs = {monitor.last_state_change_ts or 0};
            function updateDetailTimer() {{
                if (startTs === 0) return;  // Not initialized yet
                const now = Math.floor(Date.now() / 1000);
                const s = Math.max(0, now - startTs);
                const h = Math.floor(s / 3600);
                const m = Math.floor((s % 3600) / 60);
                const sec = s % 60;
                const str = `${{h}}h ${{m}}m ${{sec}}s`;
                document.getElementById('stat-duration').textContent = str;
                const liveDur = document.getElementById('live-duration');
                if (liveDur) liveDur.textContent = str;
            }}
            setInterval(updateDetailTimer, 1000);

            // Only initialize chart if data exists
            const chartCanvas = document.getElementById('latencyChart');
            if (chartCanvas) {{
                new Chart(chartCanvas, {{
                    type: 'line',
                    data: {{
                        labels: {json.dumps(chart_labels)},
                        datasets: [{{ label: 'Latency (ms)', data: {json.dumps(chart_data)}, borderColor: '#6366f1', fill: true, tension: 0.3, pointRadius: 0 }}]
                    }},
                    options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
                }});
            }}
        </script>
    </body>
    </html>
    """

@app.get("/status")
async def status():
    pacific = ZoneInfo("America/Los_Angeles")
    async with AsyncSessionLocal() as session:
        monitors = (await session.execute(select(Monitor))).scalars().all()
        result = []
        for m in monitors:
            if m.last_state_change_ts is not None:
                change_str = datetime.fromtimestamp(m.last_state_change_ts, tz=pacific).strftime("%m/%d %I:%M %p %Z")
            else:
                change_str = "Pending"
            result.append({
                "id": m.id,
                "url": m.url,
                "is_up": m.is_up,
                "last_state_change_ts": m.last_state_change_ts or 0,
                "last_state_change_str": change_str,
                "code_version": m.code_version
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
        
        up_checks = [c for c in checks if 0 < c.status_code < 400]
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
            async with AsyncSessionLocal() as session:
                monitors = (await session.execute(select(Monitor))).scalars().all()
                print(f"[CHECKER] Running checks for {len(monitors)} monitors...")
                await asyncio.gather(*[run_check(m.id, m.url) for m in monitors])
                print(f"[CHECKER] Checks completed")
        except Exception as e: 
            print(f"[CHECKER ERROR] {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(CHECK_INTERVAL)

FAIL_THRESHOLD = 2  # require N consecutive failures before marking DOWN

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
    is_success = 0 < code < 400

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
                if c.status_code == 0:
                    consecutive_failures += 1
                else:
                    break

        confirmed_up = is_success
        confirmed_down = (not is_success) and consecutive_failures >= FAIL_THRESHOLD

        # Fetch code version on every successful check
        if confirmed_up:
            try:
                cv = await http_client.get(f"{url}/code_version", timeout=5.0)
                if cv.status_code == 200:
                    data = cv.json()
                    build_nodes = data.get("endpoint_build_nodes", {})

                    rows = []

                    for name, node in build_nodes.items():
                        desc = node.get("description", "")
                        
                        # Get code_version from response key (for kg2c) or from description (for multiomics)
                        code_ver = node.get("code_version")
                        if not code_ver:
                            # For multiomics, extract from description
                            m_code = re.search(r"_v([\d\.]+)\.tsv", desc)
                            if m_code:
                                code_ver = m_code.group(1)
                            # For kg2c, extract from description
                            m_code = re.search(r"kg2c-([\d\.]+-v[\d\.]+)", desc)
                            if m_code:
                                code_ver = m_code.group(1)
                        
                        # Get biolink_version from response key first, then from description
                        biolink = node.get("biolink_version")
                        if not biolink:
                            m_biolink = re.search(r"Biolink version used was ([0-9\.]+)", desc)
                            if m_biolink:
                                biolink = m_biolink.group(1)
                        
                        # Extract build date from description for all types
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

                    m.code_version = "\n\n".join(rows)
            except Exception:
                pass

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