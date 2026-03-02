import asyncio
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from .database import Base, engine, AsyncSessionLocal
from .models import Monitor, Check, StateEvent

# --- Configuration ---
CHECK_INTERVAL = 30
ENDPOINTS = [
    "https://kg2cploverdb.ci.transltr.io",
    "https://kg2cploverdb.test.transltr.io",
    "https://kg2cplover3.rtx.ai:9990",
    "https://multiomics.rtx.ai:9990",
    "https://multiomics.ci.transltr.io",
]

http_client = httpx.AsyncClient(timeout=10, verify=False)

import os

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")

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
            body { font-family: sans-serif; margin: 40px; background: #f4f4f9; color: #333; }
            table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            th, td { padding: 15px; text-align: left; border-bottom: 1px solid #eee; }
            th { background-color: #6366f1; color: white; }
            tr:hover { background-color: #f9fafb; }
            .up { color: #10b981; font-weight: bold; }
            .down { color: #ef4444; font-weight: bold; }
            .pending { color: #9ca3af; font-weight: bold; font-style: italic; }
            a { color: #4f46e5; text-decoration: none; font-weight: 500; }
        </style>
    </head>
    <body>
        <h2>System Status</h2>
        <table>
            <thead>
                <tr>
                    <th>Endpoint</th>
                    <th>Status</th>
                    <th>Status Since</th>
                    <th>Duration</th>
                    <th>Code Version</th>
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
                                <td class="code-version"></td>
                            `;
                            tbody.appendChild(row);
                            rowRefs[m.url] = {
                                statusCell: row.querySelector(".status-cell"),
                                sinceCell: row.querySelector(".status-since"),
                                timeCell: row.querySelector(".state-time"),
                                codeCell: row.querySelector(".code-version"),
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
                        ref.codeCell.textContent = m.code_version || "—";
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
                        const m = Math.floor((s % 3600) / 60);
                        const sec = s % 60;
                        ref.timeCell.textContent = `${h}h ${m}m ${sec}s`;
                    }
                });
            }

            loadStatus();
            setInterval(loadStatus, 5000);
            setInterval(updateTimers, 1000);
            
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
            chart_labels.append(dt.astimezone(pacific).strftime('%I:%M %p'))
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
        current_start_dt = datetime.fromtimestamp(monitor.last_state_change_ts, tz=pacific).strftime('%m/%d %I:%M:%S %p')
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
            start_time = datetime.fromtimestamp(e.changed_at_ts, tz=pacific).strftime('%m/%d %I:%M:%S %p')
            
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
            raw_logs_list.append(f"<tr><td>{dt.astimezone(pacific).strftime('%m/%d %I:%M:%S %p')}</td><td>{c.status_code}</td></tr>")
    raw_logs = "".join(raw_logs_list)

    return f"""
    <html>
    <head>
        <title>{monitor.url}</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: sans-serif; margin: 40px; background: #f4f4f9; }}
            .container {{ max-width: 1000px; margin: auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 25px; }}
            .stat-card {{ background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 4px solid #6366f1; }}
            .stat-card h4 {{ margin: 0; font-size: 0.75em; color: #666; text-transform: uppercase; }}
            .stat-card p {{ margin: 8px 0 0; font-size: 1.2em; font-weight: bold; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
            .up {{ color: #10b981; font-weight: bold; }} .down {{ color: #ef4444; font-weight: bold; }} .pending {{ color: #9ca3af; font-style: italic; }}
            details {{ margin-bottom: 15px; border: 1px solid #eee; border-radius: 8px; padding: 10px; }}
            summary {{ font-weight: bold; cursor: pointer; padding: 5px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" style="text-decoration:none; color:#6366f1;">← Back to Dashboard</a>
            <h2 style="margin: 15px 0;">{monitor.url}</h2>
            
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
                change_str = datetime.fromtimestamp(m.last_state_change_ts, tz=pacific).strftime("%m/%d %I:%M %p")
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

        # first check
        if previous_state is None:
            m.is_up = confirmed_up
            m.last_state_change_ts = int(time.time())
            session.add(StateEvent(
                monitor_id=monitor_id,
                is_up=confirmed_up,
                changed_at_ts=m.last_state_change_ts
            ))

            # fetch code version on first successful initialization
            if confirmed_up:
                try:
                    cv = await http_client.get(f"{url}/code_version", timeout=5.0)
                    if cv.status_code == 200:
                        data = cv.json()
                        build_nodes = data.get("endpoint_build_nodes", {})
                        if build_nodes:
                            first_node = next(iter(build_nodes.values()))
                            m.code_version = first_node.get("description")
                except Exception:
                    pass

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

            # fetch code version once on recovery
            try:
                cv = await http_client.get(f"{url}/code_version", timeout=5.0)
                if cv.status_code == 200:
                    m.code_version = cv.json().get("code_info")
            except Exception:
                pass

            await send_slack_message(f"{url} is BACK UP")

        session.add(Check(
            monitor_id=monitor_id,
            status_code=code,
            response_time_ms=dur,
            error_message=error_message
        ))

        await session.commit()