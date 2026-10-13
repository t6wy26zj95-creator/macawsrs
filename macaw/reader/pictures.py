"""Real photos for the reader's word card: Wikimedia Commons first, then Openverse (both free, no key)."""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE = "https://api.openverse.org/v1/images/"
# Wikimedia asks every client to name itself.
USER_AGENT = "macaw-reader/1.0 (https://github.com/t6wy26zj95-creator/macawsrs; Telegram study bot)"


class Unreachable(Exception):
    """Wikimedia Commons couldn't be asked (network, timeout, an error page)."""


async def find(query: str, count: int = 3, api: str = API, openverse: str = OPENVERSE) -> list[dict]:
    """Photos for a search phrase: JPEGs only, so no diagrams, logos or drawings. [] when none fit.
    Raises Unreachable when Commons can't be reached."""
    # Commons searches the text of files too, so a plain search is crowded out by scanned books (PDF,
    # DjVu) that mention the words; "filetype:bitmap" keeps it to pictures. A phrase that finds nothing
    # gets a second try with its last word ("flooded river" -> "river").
    tries = [query]
    if " " in query.strip():
        tries.append(query.split()[-1])
    timeout = aiohttp.ClientTimeout(total=10)
    errors = []
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as s:
        for search, where in [(_search, api), (_openverse, openverse)]:
            try:
                for q in tries:
                    found = await search(s, where, q, count)
                    if found:
                        return found
            except Exception as e:  # network errors, timeouts (not TimeoutError on 3.10), bad JSON
                log.warning("photo search at %s failed: %s: %s", where, type(e).__name__, e)
                errors.append(e)
    if len(errors) == 2:
        raise Unreachable("; ".join(f"{type(e).__name__}: {e}" for e in errors))
    return []


async def _search(s: aiohttp.ClientSession, api: str, query: str, count: int) -> list[dict]:
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrnamespace": "6", "gsrlimit": "30", "gsrsearch": f"{query} filetype:bitmap",
        "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": "360",
    }
    async with s.get(api, params=params) as r:
        if r.status != 200:
            raise Unreachable(f"Commons answered {r.status}")
        data = await r.json(content_type=None)
    if data.get("error"):
        raise Unreachable(f"Commons error: {data['error'].get('info', data['error'])}")
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
            "source": "Wikimedia Commons",
        })
        if len(out) == count:
            break
    log.info("photos for %r: %d results, %d kept", query, len(pages), len(out))
    return out


async def _openverse(s: aiohttp.ClientSession, api: str, query: str, count: int) -> list[dict]:
    params = {"q": query, "category": "photograph", "mature": "false", "page_size": str(count * 3)}
    async with s.get(api, params=params) as r:
        if r.status != 200:
            raise Unreachable(f"Openverse answered {r.status}")
        data = await r.json(content_type=None)
    results = data.get("results") or []
    out = []
    for item in results:
        thumb = item.get("thumbnail") or ""
        w, h = item.get("width") or 0, item.get("height") or 0
        if not thumb.startswith("https://") or (w and h and (w < 300 or h < 200 or w > 3 * h or h > 2 * w)):
            continue
        out.append({
            "src": thumb, "w": w, "h": h,
            "page": item.get("foreign_landing_url") or "",
            "title": str(item.get("title") or "")[:100],
            "source": "Openverse",
        })
        if len(out) == count:
            break
    log.info("photos for %r (Openverse): %d results, %d kept", query, len(results), len(out))
    return out
