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
        html_renderer: Optional[Callable[[], str]] = None,
    ) -> None:
        self.port = int(port)
        self.host = host
        self.status_provider = status_provider or (lambda: {})
        self.on_stop = on_stop or (lambda: None)
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

        # Build a small human-readable HTML status page.
        uptime = int(time.time() - self._started_at)
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h{m}m{s}s"

        snap_json = html.escape(json.dumps(snap, default=str, indent=2))
        target = html.escape(str(snap.get("target", "—")))
        filter_str = html.escape(str(snap.get("filter_types", "—")))
        order = html.escape(str(snap.get("order", "—")))
        stopped = snap.get("stopped", False)
        status_text = "STOPPED" if stopped else "RUNNING"
        status_color = "#c62828" if stopped else "#2e7d32"
        sweep_num = snap.get("sweep_num", 0)
        items_in_sweep = snap.get("items_in_sweep", 0)
        msgs_in_sweep = snap.get("msgs_in_sweep", 0)
        skipped_in_sweep = snap.get("skipped_in_sweep", 0)
        total_items = snap.get("total_items_sent", 0)
        total_msgs = snap.get("total_msgs_sent", 0)
        total_skipped = snap.get("total_skipped", 0)
        current_id = snap.get("current_item_id")
        current_kind = snap.get("current_item_kind", "")
        upload_active = bool(snap.get("upload_active"))
        upload_total = int(snap.get("upload_total", 0))
        upload_current = int(snap.get("upload_current", 0))
        batch_pause = bool(snap.get("batch_pause_active"))
        batch_remaining = float(snap.get("batch_pause_remaining", 0))

        upload_html = ""
        if upload_active and upload_total:
            pct = upload_current / upload_total * 100
            upload_html = f"<p>↑ Upload: {pct:.1f}% ({upload_current/(1024*1024):.1f}/{upload_total/(1024*1024):.1f} MB)</p>"

        batch_html = ""
        if batch_pause:
            batch_html = f"<p>⏸ Batch pause: {batch_remaining:.0f}s remaining</p>"

        current_html = ""
        if current_id:
            current_html = f"<p>Current item: <code>msg_id={current_id}</code> [{html.escape(current_kind)}]</p>"

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Bulk Forwarder — Status</title>
  <style>
    body {{ font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #fafafa; color: #222; max-width: 800px; margin: 2em auto; padding: 0 1em; }}
    h1 {{ margin: 0 0 .5em 0; }}
    .badge {{ display: inline-block; padding: .15em .6em; border-radius: 12px;
              color: white; background: {status_color}; font-weight: 600; font-size: 12px; }}
    .row {{ display: flex; gap: 2em; flex-wrap: wrap; }}
    .card {{ background: white; padding: 1em 1.5em; border-radius: 8px;
              box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 1em; }}
    code {{ background: #f0f0f0; padding: .1em .3em; border-radius: 3px; }}
    pre {{ background: #1e1e1e; color: #d4d4d4; padding: 1em; border-radius: 6px;
            overflow: auto; font-size: 12px; }}
    a {{ color: #1976d2; }}
    .meta {{ color: #888; font-size: 12px; }}
    button {{ background: #c62828; color: white; border: none; padding: .5em 1em;
              border-radius: 6px; cursor: pointer; font-weight: 600; }}
    button:hover {{ background: #b71c1c; }}
  </style>
</head>
<body>
  <h1>Telegram Bulk Forwarder <span class="badge">{status_text}</span></h1>
  <p class="meta">uptime: {uptime_str} · requests served: {self._request_count}</p>

  <div class="card">
    <h3>Configuration</h3>
    <p>Target: <code>{target}</code></p>
    <p>Filter: <code>{filter_str}</code> · Order: <code>{order}</code></p>
  </div>

  <div class="card">
    <h3>Progress</h3>
    <div class="row">
      <div>
        <h4>Current Sweep #{sweep_num}</h4>
        <p>📦 {items_in_sweep} items</p>
        <p>📨 {msgs_in_sweep} msgs</p>
        <p>↪ {skipped_in_sweep} skipped</p>
      </div>
      <div>
        <h4>Cumulative</h4>
        <p>📦 {total_items} items</p>
        <p>📨 {total_msgs} msgs</p>
        <p>↪ {total_skipped} skipped</p>
      </div>
    </div>
    {current_html}
    {upload_html}
    {batch_html}
  </div>

  <div class="card">
    <h3>Controls</h3>
    <p>
      <button onclick="if(confirm('Stop the bot?')) fetch('/stop',{{method:'POST'}}).then(()=>location.reload())">
        Stop bot
      </button>
    </p>
    <p class="meta">
      Also available:
      <code>curl -X POST {html.escape("$HOST/stop")}</code>
      or send <code>/stop</code> to your Saved Messages.
    </p>
  </div>

  <div class="card">
    <h3>Raw JSON</h3>
    <pre>{snap_json}</pre>
  </div>

  <p class="meta">Auto-refresh in 10s… <script>setTimeout(()=>location.reload(),10000)</script></p>
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
