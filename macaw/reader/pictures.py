"""Real photos for the reader's word card, from Wikimedia Commons (no key needed)."""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia asks every client to name itself.
USER_AGENT = "macaw-reader/1.0 (https://github.com/t6wy26zj95-creator/macawsrs; Telegram study bot)"


async def find(query: str, count: int = 3, api: str = API) -> list[dict]:
    """Photos (JPEG only, so no diagrams, logos or drawings) for a search phrase; [] when none or offline."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrnamespace": "6", "gsrlimit": str(count * 2),
        "gsrsearch": f"{query} filemime:image/jpeg",
        "prop": "imageinfo", "iiprop": "url|size", "iiurlwidth": "360",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as s:
            async with s.get(api, params=params) as r:
                if r.status != 200:
                    log.warning("picture search answered %s", r.status)
                    return []
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        log.warning("picture search failed: %s", e)
        return []
    pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl") or ""
        # Skip tiny pictures and very long strips (panoramas, scans of pages).
        w, h = info.get("width") or 0, info.get("height") or 0
        if not thumb.startswith("https://upload.wikimedia.org/") or w < 300 or h < 200 or w > 3 * h or h > 2 * w:
            continue
        out.append({
            "src": thumb,
            "w": info.get("thumbwidth"), "h": info.get("thumbheight"),
            "page": info.get("descriptionurl") or "",
            "title": str(p.get("title", "")).removeprefix("File:").rsplit(".", 1)[0],
        })
        if len(out) == count:
            break
    return out
