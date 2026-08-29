"""Unified dashboard — shows stats for both bots and allows control.

Runs on port 8080. Proxies requests to:
  - Forwarder Bot: http://forwarder-bot:8081/stats
  - Saved Forwarder: http://saved-forwarder:10000/status
"""
import asyncio
import json
import logging
import os
from aiohttp import web, ClientSession

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")

# Bot endpoints (Docker container names)
FORWARDER_STATS_URL = os.environ.get("FORWARDER_STATS_URL",
                                      "http://forwarder-bot:8081/stats")
FORWARDER_STOP_SCRAPE_URL = os.environ.get("FORWARDER_STOP_SCRAPE_URL",
                                           "http://forwarder-bot:8081/stop_scrape")
FORWARDER_CANCEL_CAPTION_URL = os.environ.get("FORWARDER_CANCEL_CAPTION_URL",
                                              "http://forwarder-bot:8081/cancel_caption")
SAVED_FORWARDER_STATUS_URL = os.environ.get("SAVED_FORWARDER_STATUS_URL",
                                             "http://saved-forwarder:10000/status")
SAVED_FORWARDER_STOP_URL = os.environ.get("SAVED_FORWARDER_STOP_URL",
                                          "http://saved-forwarder:10000/stop")
SAVED_FORWARDER_RESET_URL = os.environ.get("SAVED_FORWARDER_RESET_URL",
                                           "http://saved-forwarder:10000/reset")

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))


async def fetch_json(url: str, timeout: float = 3.0) -> dict | None:
    """Fetch JSON from a URL with a short timeout. Returns None on error."""
    try:
        async with ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
    except Exception:
        return None


async def post_json(url: str, timeout: float = 5.0) -> dict | None:
    """POST to a URL with a short timeout. Returns None on error."""
    try:
        async with ClientSession() as session:
            async with session.post(url, timeout=timeout) as resp:
                try:
                    return await resp.json()
                except Exception:
                    return {"ok": False, "status": resp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----- API handlers -----

async def api_stats(request: web.Request) -> web.Response:
    """GET /api/stats — fetch stats from both bots in parallel."""
    forwarder_task = asyncio.create_task(fetch_json(FORWARDER_STATS_URL))
    saved_task = asyncio.create_task(fetch_json(SAVED_FORWARDER_STATUS_URL))

    forwarder_stats = await forwarder_task
    saved_stats = await saved_task

    return web.json_response({
        "forwarder": forwarder_stats,
        "saved_forwarder": saved_stats,
        "forwarder_online": forwarder_stats is not None,
        "saved_forwarder_online": saved_stats is not None,
    })


async def api_stop_scrape(request: web.Request) -> web.Response:
    """POST /api/stop_scrape — stop the active scrape on the forwarder bot."""
    result = await post_json(FORWARDER_STOP_SCRAPE_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"})


async def api_cancel_caption(request: web.Request) -> web.Response:
    """POST /api/cancel_caption — clear the custom caption."""
    result = await post_json(FORWARDER_CANCEL_CAPTION_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"})


async def api_stop_saved(request: web.Request) -> web.Response:
    """POST /api/stop_saved — stop the saved-forwarder bot."""
    result = await post_json(SAVED_FORWARDER_STOP_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach saved forwarder"})


async def api_reset_saved(request: web.Request) -> web.Response:
    """POST /api/reset_saved — reset the saved-forwarder watermark."""
    result = await post_json(SAVED_FORWARDER_RESET_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach saved forwarder"})


async def api_health(request: web.Request) -> web.Response:
    """GET /api/health — simple health check."""
    return web.json_response({"ok": True, "service": "dashboard"})


# ----- HTML dashboard -----

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Telegram Bots Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #0f0f0f; color: #e0e0e0; padding: 20px; }
        .header { text-align: center; margin-bottom: 30px; }
        .header h1 { font-size: 24px; color: #fff; }
        .header .subtitle { color: #888; font-size: 14px; margin-top: 5px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
                max-width: 1200px; margin: 0 auto; }
        @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
        .card { background: #1a1a1a; border-radius: 12px; padding: 20px;
                border: 1px solid #333; }
        .card h2 { font-size: 18px; margin-bottom: 15px; display: flex;
                   align-items: center; gap: 8px; }
        .status-dot { width: 12px; height: 12px; border-radius: 50%;
                      display: inline-block; }
        .status-online { background: #4caf50; box-shadow: 0 0 8px #4caf50; }
        .status-offline { background: #f44336; box-shadow: 0 0 8px #f44336; }
        .status-unknown { background: #666; }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .stat-item { background: #222; padding: 12px; border-radius: 8px; }
        .stat-label { font-size: 12px; color: #888; text-transform: uppercase;
                     letter-spacing: 0.5px; }
        .stat-value { font-size: 20px; font-weight: 600; margin-top: 4px; color: #fff; }
        .stat-value.small { font-size: 14px; }
        .section-title { font-size: 14px; color: #888; margin: 15px 0 8px;
                         text-transform: uppercase; letter-spacing: 0.5px; }
        .progress-bar { width: 100%; height: 6px; background: #333;
                        border-radius: 3px; margin-top: 8px; overflow: hidden; }
        .progress-fill { height: 100%; background: #4caf50; transition: width 0.5s;
                         border-radius: 3px; }
        .btn { padding: 8px 16px; border: none; border-radius: 8px; cursor: pointer;
               font-size: 13px; font-weight: 600; transition: all 0.2s; }
        .btn-stop { background: #f44336; color: #fff; }
        .btn-stop:hover { background: #d32f2f; }
        .btn-warn { background: #ff9800; color: #000; }
        .btn-warn:hover { background: #f57c00; }
        .btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-row { display: flex; gap: 8px; margin-top: 15px; flex-wrap: wrap; }
        .scrape-active { border-color: #4caf50; box-shadow: 0 0 12px rgba(76,175,80,0.2); }
        .footer { text-align: center; margin-top: 30px; color: #555; font-size: 12px; }
        .refresh-indicator { display: inline-block; width: 10px; height: 10px;
                            border-radius: 50%; background: #4caf50; margin-left: 8px;
                            animation: pulse 1s infinite; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
    </style>
</head>
<body>
    <div class="header">
        <h1>Telegram Bots Dashboard</h1>
        <div class="subtitle">Unified monitoring & control <span class="refresh-indicator"></span></div>
    </div>

    <div class="grid">
        <!-- Forwarder Bot -->
        <div class="card" id="forwarder-card">
            <h2>
                <span class="status-dot status-unknown" id="forwarder-dot"></span>
                Forwarder Bot
            </h2>
            <div id="forwarder-content">
                <p style="color:#666">Loading...</p>
            </div>
        </div>

        <!-- Saved Forwarder -->
        <div class="card" id="saved-card">
            <h2>
                <span class="status-dot status-unknown" id="saved-dot"></span>
                Saved Messages Forwarder
            </h2>
            <div id="saved-content">
                <p style="color:#666">Loading...</p>
            </div>
        </div>
    </div>

    <div class="footer">
        Auto-refreshing every 5 seconds · <span id="last-update">--</span>
    </div>

    <script>
        async function fetchStats() {
            try {
                const resp = await fetch('/api/stats');
                const data = await resp.json();
                updateForwarder(data);
                updateSaved(data);
                document.getElementById('last-update').textContent =
                    new Date().toLocaleTimeString();
            } catch (e) {
                console.error('Fetch failed:', e);
            }
        }

        function updateForwarder(data) {
            const dot = document.getElementById('forwarder-dot');
            const content = document.getElementById('forwarder-content');
            const card = document.getElementById('forwarder-card');

            if (!data.forwarder_online) {
                dot.className = 'status-dot status-offline';
                card.classList.remove('scrape-active');
                content.innerHTML = '<p style="color:#f44336">Bot is offline or not responding</p>';
                return;
            }

            dot.className = 'status-dot status-online';
            const f = data.forwarder || {};

            // Scrape section
            let scrapeHtml = '';
            if (f.scrape_running) {
                card.classList.add('scrape-active');
                const s = f.scrape || {};
                const pct = s.total_seen > 0 ? (s.sent_count / s.total_seen * 100).toFixed(1) : 0;
                scrapeHtml = `
                    <div class="section-title">Active Scrape</div>
                    <div class="stats-grid">
                        <div class="stat-item">
                            <div class="stat-label">Source</div>
                            <div class="stat-value small">${s.source_ref || '?'}</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-label">Destination</div>
                            <div class="stat-value small">${s.dest_label || '?'}</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-label">Total Seen</div>
                            <div class="stat-value">${s.total_seen || 0}</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-label">Sent</div>
                            <div class="stat-value" style="color:#4caf50">${s.sent_count || 0}</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-label">Failed</div>
                            <div class="stat-value" style="color:#f44336">${s.failed_count || 0}</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-label">Skipped</div>
                            <div class="stat-value" style="color:#888">${s.skipped_count || 0}</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-label">Flood Waits</div>
                            <div class="stat-value" style="color:#ff9800">${s.flood_waits || 0}</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-label">Elapsed</div>
                            <div class="stat-value">${Math.floor(s.elapsed_sec || 0)}s</div>
                        </div>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width:${pct}%"></div>
                    </div>
                    <div class="btn-row">
                        <button class="btn btn-stop" onclick="stopScrape()">Stop Scrape</button>
                    </div>
                `;
            } else {
                card.classList.remove('scrape-active');
                if (f.scrape && f.scrape.total_seen > 0) {
                    const s = f.scrape;
                    scrapeHtml = `
                        <div class="section-title">Last Scrape (finished)</div>
                        <div class="stats-grid">
                            <div class="stat-item">
                                <div class="stat-label">Total Sent</div>
                                <div class="stat-value" style="color:#4caf50">${s.sent_count || 0}</div>
                            </div>
                            <div class="stat-item">
                                <div class="stat-label">Total Failed</div>
                                <div class="stat-value" style="color:#f44336">${s.failed_count || 0}</div>
                            </div>
                        </div>
                    `;
                } else {
                    scrapeHtml = '<div class="section-title">No scrape has been run</div>';
                }
            }

            // Caption
            let captionHtml = '';
            if (f.custom_caption !== null && f.custom_caption !== undefined) {
                const cap = f.custom_caption;
                if (cap === '') {
                    captionHtml = `<div class="stat-item" style="margin-top:10px">
                        <div class="stat-label">Caption Mode</div>
                        <div class="stat-value small" style="color:#ff9800">STRIP (no captions)</div>
                    </div>`;
                } else {
                    const preview = cap.length > 50 ? cap.substring(0,50) + '...' : cap;
                    captionHtml = `<div class="stat-item" style="margin-top:10px">
                        <div class="stat-label">Custom Caption</div>
                        <div class="stat-value small" style="color:#4caf50">${preview}</div>
                    </div>`;
                }
            } else {
                captionHtml = `<div class="stat-item" style="margin-top:10px">
                    <div class="stat-label">Caption Mode</div>
                    <div class="stat-value small" style="color:#888">Original captions</div>
                </div>`;
            }

            content.innerHTML = `
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-label">Bot</div>
                        <div class="stat-value small">@${f.bot_name || '?'}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Telethon</div>
                        <div class="stat-value small" style="color:${f.telethon === 'connected' ? '#4caf50' : '#f44336'}">
                            ${f.telethon || 'unknown'}
                        </div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Destination</div>
                        <div class="stat-value small">${f.destination_chat_title || f.destination_group || 'Not set'}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Forum</div>
                        <div class="stat-value small">${f.destination_is_forum ? 'Yes' : 'No'}</div>
                    </div>
                </div>
                ${captionHtml}
                <div class="btn-row">
                    <button class="btn btn-warn" onclick="cancelCaption()">Clear Caption</button>
                </div>
                ${scrapeHtml}
            `;
        }

        function updateSaved(data) {
            const dot = document.getElementById('saved-dot');
            const content = document.getElementById('saved-content');

            if (!data.saved_forwarder_online) {
                dot.className = 'status-dot status-offline';
                content.innerHTML = '<p style="color:#f44336">Bot is offline or not responding</p>';
                return;
            }

            dot.className = 'status-dot status-online';
            const s = data.saved_forwarder || {};

            // The second bot's actual /status JSON fields (from forwarder.py):
            //   stopped, sweep_num, items_in_sweep, total_items_sent, total_msgs_sent,
            //   total_skipped, current_item_id, batch_pause_active, batch_pause_remaining,
            //   batch_pause_total, last_offset_id, state_sent_ids_count,
            //   filter_types, order, batch_size, items_in_batch, target
            const stopped = s.stopped || false;
            const running = !stopped;
            const sweep = s.sweep_num || 0;
            const sentItems = s.total_items_sent || 0;
            const sentMsgs = s.total_msgs_sent || 0;
            const skipped = s.total_skipped || 0;
            const itemsInSweep = s.items_in_sweep || 0;
            const lastId = s.last_offset_id || 0;
            const stateCount = s.state_sent_ids_count || 0;
            const filterTypes = (s.filter_types || []).join(', ') || 'all';
            const order = s.order || 'old';
            const batchSize = s.batch_size || 30;
            const itemsInBatch = s.items_in_batch || 0;
            const batchPct = Math.min(100, (itemsInBatch / batchSize) * 100).toFixed(0);
            const pauseActive = s.batch_pause_active || false;
            const pauseRemaining = (s.batch_pause_remaining || 0).toFixed(0);
            const currentItemId = s.current_item_id || null;
            const currentItemKind = s.current_item_kind || '';
            const uploadActive = s.upload_active || false;
            const uploadPct = s.upload_total > 0 ?
                ((s.upload_current / s.upload_total) * 100).toFixed(1) : 0;

            content.innerHTML = `
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-label">Status</div>
                        <div class="stat-value small" style="color:${running ? '#4caf50' : '#f44336'}">
                            ${running ? 'Running' : 'Stopped'}
                        </div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Sweep #</div>
                        <div class="stat-value">${sweep}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Total Items Sent</div>
                        <div class="stat-value" style="color:#4caf50">${sentItems}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Total Messages</div>
                        <div class="stat-value">${sentMsgs}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Skipped</div>
                        <div class="stat-value" style="color:#ff9800">${skipped}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Watermark</div>
                        <div class="stat-value">${lastId}</div>
                    </div>
                </div>
                <div class="section-title">Current Activity</div>
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-label">Items in Sweep</div>
                        <div class="stat-value">${itemsInSweep}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">In State</div>
                        <div class="stat-value small">${stateCount} IDs</div>
                    </div>
                </div>
                ${currentItemId ? `
                <div class="stat-item" style="margin-top:8px">
                    <div class="stat-label">Current Item</div>
                    <div class="stat-value small">msg #${currentItemId} (${currentItemKind})</div>
                    ${uploadActive ? `<div class="progress-bar"><div class="progress-fill" style="width:${uploadPct}%"></div></div>` : ''}
                </div>` : ''}
                ${pauseActive ? `
                <div class="stat-item" style="margin-top:8px">
                    <div class="stat-label">Batch Pause</div>
                    <div class="stat-value small" style="color:#ff9800">${pauseRemaining}s remaining</div>
                    <div class="progress-bar"><div class="progress-fill" style="width:${((s.batch_pause_total - s.batch_pause_remaining) / s.batch_pause_total * 100).toFixed(0)}%; background:#ff9800"></div></div>
                </div>` : ''}
                <div class="section-title">Config</div>
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-label">Filter</div>
                        <div class="stat-value small">${filterTypes}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Order</div>
                        <div class="stat-value small">${order}</div>
                    </div>
                </div>
                <div class="btn-row">
                    <button class="btn btn-stop" onclick="stopSaved()">Stop Bot</button>
                    <button class="btn btn-warn" onclick="resetSaved()">Reset Watermark</button>
                </div>
            `;
        }

        async function stopScrape() {
            try {
                await fetch('/api/stop_scrape', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        async function cancelCaption() {
            try {
                await fetch('/api/cancel_caption', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        async function stopSaved() {
            if (!confirm('Stop the Saved Messages Forwarder?')) return;
            try {
                await fetch('/api/stop_saved', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        async function resetSaved() {
            if (!confirm('Reset the watermark? This will re-scan from the beginning.')) return;
            try {
                await fetch('/api/reset_saved', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        // Auto-refresh
        fetchStats();
        setInterval(fetchStats, 5000);
    </script>
</body>
</html>"""


async def dashboard_handler(request: web.Request) -> web.Response:
    """Serve the HTML dashboard."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def main():
    app = web.Application()
    app.router.add_get("/", dashboard_handler)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/health", api_health)
    app.router.add_post("/api/stop_scrape", api_stop_scrape)
    app.router.add_post("/api/cancel_caption", api_cancel_caption)
    app.router.add_post("/api/stop_saved", api_stop_saved)
    app.router.add_post("/api/reset_saved", api_reset_saved)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT)
    await site.start()
    logger.info("Dashboard listening on 0.0.0.0:%d", DASHBOARD_PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
