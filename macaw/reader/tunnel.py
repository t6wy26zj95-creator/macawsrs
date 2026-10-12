"""Finds the reader's public address when it is reached through a Cloudflare
quick tunnel (deploy/reader-tunnel.sh). The tunnel needs no open ports, but its
address is random and changes whenever the tunnel restarts, so the bot asks
cloudflared for it every so often and updates the Books button."""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)


async def tunnel_url(metrics: str) -> str | None:
    """cloudflared's local status page says which trycloudflare.com address it got."""
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"http://{metrics}/quicktunnel") as r:
                if r.status != 200:
                    return None
                host = (await r.json(content_type=None)).get("hostname") or ""
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
        return None
    host = host.strip().strip("/")
    return f"https://{host}/" if host else None
