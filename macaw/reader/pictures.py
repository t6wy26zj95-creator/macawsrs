"""Real photos for the reader's word card, from Wikimedia Commons (no key needed)."""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia asks every client to name itself.
USER_AGENT = "macaw-reader/1.0 (https://github.com/t6wy26zj95-creator/macawsrs; Telegram study bot)"


class Unreachable(Exception):
    """Wikimedia Commons couldn't be asked (network, timeout, an error page)."""


async def find(query: str, count: int = 3, api: str = API) -> list[dict]:
    """Photos for a search phrase: JPEGs only, so no diagrams, logos or drawings. [] when none fit.
    Raises Unreachable when Commons can't be reached."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrnamespace": "6", "gsrlimit": "15", "gsrsearch": query,
        "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": "360",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as s:
            async with s.get(api, params=params) as r:
                if r.status != 200:
                    raise Unreachable(f"Commons answered {r.status}")
                data = await r.json(content_type=None)
    except Unreachable:
        raise
    except Exception as e:  # network errors, timeouts (not TimeoutError on 3.10), bad JSON
        raise Unreachable(f"{type(e).__name__}: {e}") from e
    pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl") or ""
        # Photos only, and not tiny pictures or very long strips (panoramas, scans of pages).
        w, h = info.get("width") or 0, info.get("height") or 0
        if info.get("mime") != "image/jpeg" or not thumb.startswith("https://upload.wikimedia.org/"):
            continue
        if w < 300 or h < 200 or w > 3 * h or h > 2 * w:
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
