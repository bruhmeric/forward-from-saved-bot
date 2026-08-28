"""
Minimal asyncio HTTP server for Render free-tier compatibility.

Render's free web services MUST:
  - Bind to 0.0.0.0:$PORT within 60 seconds of startup
  - Respond to HTTP health checks (Render pings / every ~30s)

This module provides:
  GET  /           → human-readable status page (HTML)
  GET  /health     → "OK" (Render health check)
  GET  /status     → JSON status snapshot
  POST /stop       → trigger graceful stop (same as /stop in Saved Messages)
  POST /pause      → (future) toggle pause — currently 410 Gone
  POST /resume     → (future) toggle resume — currently 410 Gone

It runs inside the same asyncio event loop as the forwarder, so the status
snapshot is always live. No external dependencies (no aiohttp / FastAPI).
"""
from __future__ import annotations

import asyncio
import html
import json
import time
from typing import Awaitable, Callable, Optional

# The HTTP server is generic; the route handlers are wired by the caller.


class WebServer:
    def __init__(
        self,
        port: int,
        host: str = "0.0.0.0",
        status_provider: Optional[Callable[[], dict]] = None,
        on_stop: Optional[Callable[[], None]] = None,
        on_reset: Optional[Callable[[], None]] = None,
        html_renderer: Optional[Callable[[], str]] = None,
    ) -> None:
        self.port = int(port)
        self.host = host
        self.status_provider = status_provider or (lambda: {})
        self.on_stop = on_stop or (lambda: None)
        self.on_reset = on_reset  # may be None
        self.html_renderer = html_renderer
        self._server: Optional[asyncio.AbstractServer] = None
        self._started_at = time.time()
        self._request_count = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port,
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        print(f"[web] listening on {addrs}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
            print("[web] server stopped")

    # ------------------------------------------------------------------
    # HTTP handler
    # ------------------------------------------------------------------

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._request_count += 1
        peer = writer.get_extra_info("peername")
        try:
            # Read the request line + headers.
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return
            try:
                parts = request_line.decode("latin-1").strip().split()
                method, path, _ver = parts
            except ValueError:
                await self._send(writer, 400, "text/plain", "Bad Request\n")
                return

            # Drain headers (up to 4 KB total).
            total = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                total += len(line)
                if not line or line in (b"\r\n", b"\n") or total > 64 * 1024:
                    break

            # Strip query string.
            path_only = path.split("?", 1)[0]

            await self._route(writer, method, path_only, peer)

        except asyncio.TimeoutError:
            try:
                await self._send(writer, 408, "text/plain", "Request Timeout\n")
            except Exception:
                pass
        except Exception as e:
            print(f"[web] handler error from {peer}: {e!r}")
            try:
                await self._send(writer, 500, "text/plain", f"Internal Error: {e}\n")
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _route(self, writer, method: str, path: str, peer) -> None:
        # Health checks (must be fast & dependency-free).
        if path in ("/health", "/healthz", "/ping") and method == "GET":
            await self._send(writer, 200, "text/plain", "OK\n")
            return

        if path == "/" and method == "GET":
            body = self._render_index_html()
            await self._send(writer, 200, "text/html; charset=utf-8", body)
            return

        if path == "/status" and method == "GET":
            try:
                snap = self.status_provider() or {}
            except Exception as e:
                await self._send(writer, 500, "application/json",
                                 json.dumps({"error": str(e)}))
                return
            await self._send(writer, 200, "application/json",
                             json.dumps(snap, default=str, indent=2))
            return

        if path == "/stop" and method in ("POST", "GET"):
            try:
                self.on_stop()
                msg = "stop signal received — bot will halt after current item\n"
            except Exception as e:
                msg = f"stop failed: {e}\n"
            await self._send(writer, 200, "text/plain", msg)
            return

        if path in ("/pause", "/resume") and method == "POST":
            await self._send(writer, 410, "text/plain",
                             "not implemented yet — use /stop\n")
            return

        if path == "/reset" and method in ("POST", "GET"):
            # Reset the auto-resume watermark so the next sweep re-scans from the beginning.
            if self.on_reset is not None:
                try:
                    self.on_reset()
                    msg = "✅ auto-resume watermark reset — next sweep will re-scan from id=1\n"
                except Exception as e:
                    msg = f"reset failed: {e}\n"
            else:
                msg = "reset handler not configured\n"
            await self._send(writer, 200, "text/plain", msg)
            return

        if path == "/favicon.ico" and method == "GET":
            await self._send(writer, 204, "text/plain", "")
            return

        await self._send(writer, 404, "text/plain",
                         f"Not Found: {method} {path}\n"
                         f"Routes: GET /, GET /health, GET /status, POST /stop\n")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_index_html(self) -> str:
        try:
            snap = self.status_provider() or {}
        except Exception as e:
            snap = {"error": str(e)}

        # Build a modern dashboard with live AJAX updates.
        uptime = int(time.time() - self._started_at)
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h{m}m{s}s"

        snap_json_escaped = html.escape(json.dumps(snap, default=str, indent=2))
        # Initial values for SSR; JS will update them live via /status polling.
        target = html.escape(str(snap.get("target", "—")))
        filter_str = html.escape(str(snap.get("filter_types", "—")))
        order = html.escape(str(snap.get("order", "—")))
        stopped = snap.get("stopped", False)
        # state_sent_count is also used to compute "remaining" in JS.
        state_sent_count = int(snap.get("state_sent_ids_count", 0))
        last_offset_id = int(snap.get("last_offset_id", 0))

        # Initial render of dynamic fields — JS will overwrite these on first poll.
        status_text = "STOPPED" if stopped else "RUNNING"
        status_dot_class = "stopped" if stopped else "running"

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Bulk Forwarder — Dashboard</title>
  <style>
    :root {{
      --bg: #0f1419;
      --panel: #1a1f2e;
      --panel-2: #232938;
      --border: #2d3548;
      --text: #e4e6eb;
      --text-dim: #8b92a5;
      --accent: #5b9bf5;
      --green: #4ade80;
      --red: #f87171;
      --yellow: #fbbf24;
      --blue: #60a5fa;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: var(--bg); color: var(--text); min-height: 100vh; padding: 1em;
    }}
    .container {{ max-width: 1100px; margin: 0 auto; }}

    /* Header */
    .header {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 1em 1.5em; background: var(--panel);
      border: 1px solid var(--border); border-radius: 12px; margin-bottom: 1em;
    }}
    .header-left {{ display: flex; align-items: center; gap: 0.75em; }}
    .header h1 {{ font-size: 1.25em; font-weight: 600; }}
    .header-sub {{ color: var(--text-dim); font-size: 0.8em; margin-top: 0.2em; }}
    .status-pill {{
      display: inline-flex; align-items: center; gap: 0.5em;
      padding: 0.4em 0.9em; border-radius: 20px;
      background: rgba(74, 222, 128, 0.12); color: var(--green);
      font-weight: 600; font-size: 0.8em; border: 1px solid rgba(74, 222, 128, 0.3);
    }}
    .status-pill.stopped {{
      background: rgba(248, 113, 113, 0.12); color: var(--red);
      border-color: rgba(248, 113, 113, 0.3);
    }}
    .status-dot {{
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--green); animation: pulse 2s infinite;
    }}
    .status-pill.stopped .status-dot {{ background: var(--red); animation: none; }}
    @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}

    /* Grid layout */
    .grid {{ display: grid; gap: 1em; grid-template-columns: 1fr 1fr; }}
    @media (max-width: 700px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .card {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 12px; padding: 1.25em 1.5em;
    }}
    .card-full {{ grid-column: 1 / -1; }}
    .card h3 {{
      font-size: 0.75em; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.05em; color: var(--text-dim); margin-bottom: 1em;
    }}

    /* Stat tiles */
    .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.75em; }}
    .stat {{
      background: var(--panel-2); border-radius: 8px; padding: 0.9em 1em;
      border: 1px solid var(--border);
    }}
    .stat-value {{ font-size: 1.6em; font-weight: 700; line-height: 1.1; }}
    .stat-label {{ font-size: 0.7em; color: var(--text-dim); text-transform: uppercase;
                  letter-spacing: 0.05em; margin-top: 0.3em; }}
    .stat.accent .stat-value {{ color: var(--accent); }}
    .stat.green .stat-value {{ color: var(--green); }}
    .stat.yellow .stat-value {{ color: var(--yellow); }}

    /* Progress bars */
    .progress-row {{
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 0.5em; font-size: 0.85em;
    }}
    .progress-row .label {{ color: var(--text-dim); }}
    .progress-row .value {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
    .progress-bar {{
      height: 8px; background: var(--panel-2); border-radius: 4px;
      overflow: hidden; margin-bottom: 1em;
    }}
    .progress-fill {{
      height: 100%; background: var(--accent); border-radius: 4px;
      transition: width 0.5s ease;
    }}
    .progress-fill.green {{ background: var(--green); }}
    .progress-fill.yellow {{ background: var(--yellow); }}

    /* Current item */
    .current-item {{
      background: var(--panel-2); border-radius: 8px; padding: 0.9em 1em;
      border-left: 3px solid var(--accent); margin-top: 0.5em;
    }}
    .current-item.idle {{ border-left-color: var(--text-dim); opacity: 0.6; }}
    .current-item .ci-row {{
      display: flex; justify-content: space-between; align-items: center;
      font-size: 0.85em;
    }}
    .current-item .ci-label {{ color: var(--text-dim); }}
    .current-item .ci-value {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
    code {{ background: var(--bg); padding: 0.15em 0.4em; border-radius: 4px;
            font-size: 0.85em; color: var(--blue); }}

    /* Controls */
    .controls {{ display: flex; gap: 0.75em; flex-wrap: wrap; }}
    button {{
      flex: 1; min-width: 140px;
      padding: 0.75em 1em; border: none; border-radius: 8px;
      font-size: 0.9em; font-weight: 600; cursor: pointer;
      transition: all 0.15s ease; color: white;
    }}
    button:hover {{ transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
    button:active {{ transform: translateY(0); }}
    button.stop {{ background: var(--red); }}
    button.stop:hover {{ background: #ef4444; }}
    button.reset {{ background: var(--blue); }}
    button.reset:hover {{ background: #3b82f6; }}

    /* Config display */
    .config-grid {{ display: grid; grid-template-columns: auto 1fr; gap: 0.5em 1.5em; font-size: 0.85em; }}
    .config-grid .ck {{ color: var(--text-dim); }}
    .config-grid .cv {{ font-weight: 500; font-variant-numeric: tabular-nums; }}

    /* JSON viewer (collapsible) */
    details {{ margin-top: 0.5em; }}
    summary {{
      cursor: pointer; color: var(--text-dim); font-size: 0.8em;
      padding: 0.5em 0; user-select: none;
    }}
    summary:hover {{ color: var(--text); }}
    pre {{
      background: var(--bg); color: #a3d4a3; padding: 1em; border-radius: 8px;
      overflow: auto; font-size: 0.75em; line-height: 1.4;
      font-family: 'SF Mono', Monaco, Consolas, monospace;
      max-height: 400px; overflow-y: auto;
    }}

    .footer {{
      text-align: center; color: var(--text-dim); font-size: 0.75em;
      padding: 1em 0; margin-top: 1em;
    }}
    .footer a {{ color: var(--accent); text-decoration: none; }}
  </style>
</head>
<body>
  <div class="container">

    <!-- Header -->
    <div class="header">
      <div class="header-left">
        <div>
          <h1>📡 Telegram Bulk Forwarder</h1>
          <div class="header-sub">
            uptime <span id="uptime">{uptime_str}</span> ·
            requests <span id="req-count">{self._request_count}</span> ·
            last sync <span id="last-sync">never</span>
          </div>
        </div>
      </div>
      <div id="status-pill" class="status-pill {status_dot_class}">
        <span class="status-dot"></span>
        <span id="status-text">{status_text}</span>
      </div>
    </div>

    <!-- Top stats -->
    <div class="grid">
      <div class="card">
        <h3>📦 This Sweep</h3>
        <div class="stats">
          <div class="stat accent">
            <div class="stat-value" id="sweep-num">#{snap.get('sweep_num', 0)}</div>
            <div class="stat-label">Sweep</div>
          </div>
          <div class="stat">
            <div class="stat-value" id="items-in-sweep">{snap.get('items_in_sweep', 0)}</div>
            <div class="stat-label">Items sent</div>
          </div>
          <div class="stat yellow">
            <div class="stat-value" id="skipped-in-sweep">{snap.get('skipped_in_sweep', 0)}</div>
            <div class="stat-label">Skipped</div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>📊 Cumulative</h3>
        <div class="stats">
          <div class="stat green">
            <div class="stat-value" id="total-items">{snap.get('total_items_sent', 0)}</div>
            <div class="stat-label">Items sent</div>
          </div>
          <div class="stat">
            <div class="stat-value" id="total-msgs">{snap.get('total_msgs_sent', 0)}</div>
            <div class="stat-label">Messages</div>
          </div>
          <div class="stat yellow">
            <div class="stat-value" id="total-skipped">{snap.get('total_skipped', 0)}</div>
            <div class="stat-label">Skipped</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Progress + current item -->
    <div class="grid">
      <div class="card">
        <h3>⚡ Current Burst</h3>
        <div id="burst-info">
          <div class="progress-row">
            <span class="label">Batch progress</span>
            <span class="value" id="batch-progress-text">—</span>
          </div>
          <div class="progress-bar"><div id="batch-progress-fill" class="progress-fill" style="width:0%"></div></div>

          <div class="progress-row">
            <span class="label">Pause countdown</span>
            <span class="value" id="pause-countdown">—</span>
          </div>
          <div class="progress-bar"><div id="pause-progress-fill" class="progress-fill yellow" style="width:0%"></div></div>
        </div>

        <div class="current-item idle" id="current-item">
          <div class="ci-row">
            <span class="ci-label">Current item</span>
            <span class="ci-value" id="ci-status">idle</span>
          </div>
          <div class="ci-row" style="margin-top:0.3em">
            <span class="ci-label">msg_id</span>
            <span class="ci-value"><code id="ci-id">—</code></span>
          </div>
          <div class="ci-row" style="margin-top:0.3em">
            <span class="ci-label">kind</span>
            <span class="ci-value" id="ci-kind">—</span>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>⚙️ Configuration</h3>
        <div class="config-grid">
          <span class="ck">Target</span><span class="cv"><code>{target}</code></span>
          <span class="ck">Filter</span><span class="cv">{filter_str}</span>
          <span class="ck">Order</span><span class="cv">{order}</span>
          <span class="ck">Watermark</span><span class="cv" id="watermark">last_offset_id={last_offset_id}</span>
          <span class="ck">In state</span><span class="cv" id="state-count">{state_sent_count} ids</span>
        </div>
      </div>
    </div>

    <!-- Controls -->
    <div class="card card-full">
      <h3>🎛️ Controls</h3>
      <div class="controls">
        <button class="stop" onclick="stopBot()">⏹ Stop bot</button>
        <button class="reset" onclick="resetWatermark()">↻ Reset watermark</button>
      </div>
      <p style="color:var(--text-dim); font-size:0.75em; margin-top:1em; line-height:1.6">
        <strong>Stop bot</strong>: halts gracefully after the current item (POST /stop)<br>
        <strong>Reset watermark</strong>: next sweep re-scans from id=1 — items already in <code>sent_ids</code> are still skipped (POST /reset)
      </p>
    </div>

    <!-- Raw JSON (collapsible) -->
    <div class="card card-full">
      <h3>📜 Raw JSON status</h3>
      <details>
        <summary>Click to expand raw JSON (live-updated)</summary>
        <pre id="raw-json">{snap_json_escaped}</pre>
      </details>
    </div>

    <div class="footer">
      Telegram Bulk Forwarder · <a href="/health">health</a> · <a href="/status">JSON</a> ·
      auto-refresh every 2s · <span id="footer-time"></span>
    </div>

  </div>

  <script>
    // Live AJAX polling — update dashboard every 2s without full page reload.
    let pollCount = 0;
    async function poll() {{
      try {{
        const res = await fetch('/status');
        const s = await res.json();
        pollCount++;
        const now = new Date();
        const ts = now.toLocaleTimeString();

        // Status pill
        const stopped = s.stopped;
        const pill = document.getElementById('status-pill');
        pill.className = 'status-pill ' + (stopped ? 'stopped' : 'running');
        document.getElementById('status-text').textContent = stopped ? 'STOPPED' : 'RUNNING';

        // Header sub
        document.getElementById('last-sync').textContent = ts;
        document.getElementById('req-count').textContent = s.req_count || pollCount;

        // Stats
        document.getElementById('sweep-num').textContent = '#' + (s.sweep_num || 0);
        document.getElementById('items-in-sweep').textContent = s.items_in_sweep || 0;
        document.getElementById('skipped-in-sweep').textContent = s.skipped_in_sweep || 0;
        document.getElementById('total-items').textContent = s.total_items_sent || 0;
        document.getElementById('total-msgs').textContent = s.total_msgs_sent || 0;
        document.getElementById('total-skipped').textContent = s.total_skipped || 0;

        // Batch progress (use items_in_batch / batch_size if available, else 0)
        const batchSize = s.batch_size || 30;
        const itemsInBatch = s.items_in_batch || (s.batch_pause_active ? batchSize : 0);
        const batchPct = Math.min(100, (itemsInBatch / batchSize) * 100);
        document.getElementById('batch-progress-fill').style.width = batchPct + '%';
        document.getElementById('batch-progress-text').textContent =
          itemsInBatch + ' / ' + batchSize + ' msgs (' + batchPct.toFixed(0) + '%)';

        // Pause countdown
        if (s.batch_pause_active) {{
          const remaining = s.batch_pause_remaining || 0;
          const total = s.batch_pause_total || 60;
          const pausePct = ((total - remaining) / total) * 100;
          document.getElementById('pause-progress-fill').style.width = pausePct + '%';
          document.getElementById('pause-countdown').textContent = remaining.toFixed(0) + 's remaining';
        }} else {{
          document.getElementById('pause-progress-fill').style.width = '0%';
          document.getElementById('pause-countdown').textContent = 'not pausing';
        }}

        // Current item
        const ci = document.getElementById('current-item');
        if (s.current_item_id) {{
          ci.classList.remove('idle');
          document.getElementById('ci-status').textContent = 'sending';
          document.getElementById('ci-id').textContent = s.current_item_id;
          document.getElementById('ci-kind').textContent = s.current_item_kind || '—';
        }} else {{
          ci.classList.add('idle');
          document.getElementById('ci-status').textContent = 'idle';
          document.getElementById('ci-id').textContent = '—';
          document.getElementById('ci-kind').textContent = '—';
        }}

        // Watermark + state count
        document.getElementById('watermark').textContent = 'last_offset_id=' + (s.last_offset_id || 0);
        document.getElementById('state-count').textContent = (s.state_sent_ids_count || 0) + ' ids';

        // Raw JSON (re-render only if expanded to avoid scroll reset)
        const details = document.querySelector('details');
        if (details.open) {{
          document.getElementById('raw-json').textContent = JSON.stringify(s, null, 2);
        }}

        document.getElementById('footer-time').textContent = ts;
      }} catch (e) {{
        console.error('poll failed:', e);
      }}
    }}

    function stopBot() {{
      if (confirm('Stop the bot? It will halt after the current item.')) {{
        fetch('/stop', {{method: 'POST'}}).then(() => setTimeout(poll, 500));
      }}
    }}
    function resetWatermark() {{
      if (confirm('Reset auto-resume watermark?\\nNext sweep will re-scan from id=1.\\nItems already in sent_ids will still be skipped.')) {{
        fetch('/reset', {{method: 'POST'}}).then(() => setTimeout(poll, 500));
      }}
    }}

    // Initial poll + interval.
    poll();
    setInterval(poll, 2000);
  </script>
</body>
</html>
"""

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    async def _send(self, writer: asyncio.StreamWriter, status: int,
                    content_type: str, body: str) -> None:
        if isinstance(body, str):
            body_bytes = body.encode("utf-8")
        else:
            body_bytes = body
        reason = {
            200: "OK", 204: "No Content", 400: "Bad Request",
            404: "Not Found", 408: "Request Timeout", 410: "Gone",
            500: "Internal Server Error",
        }.get(status, "OK")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(headers)
        writer.write(body_bytes)
        await writer.drain()
