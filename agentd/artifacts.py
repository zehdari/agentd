"""Artifact pages: an agent publishes a page with `agentd-artifact` and shares its unguessable link.

Each one lives in STATE_DIR/artifacts/<slug>/site and is served at /a/<slug>/. The link is the key, so
this is the one handler the dashboard's Tailscale user check lets through. Pages share an origin with
the dashboard, whose API drives agents that have sudo, so every response carries a CSP sandbox: the
page gets an opaque origin and can neither read nor call /api."""
import json
import re
from pathlib import Path

from aiohttp import web

from . import config

ROOT = config.STATE_DIR / "artifacts"
STATIC = Path(__file__).parent / "web"
THEME = {"artifact.css", "artifact.js", "fonts/public-sans-latin-wght-normal.woff2",   # agentd's look, at /a/theme/
         "fonts/public-sans-latin-wght-italic.woff2", "fonts/jetbrains-mono-latin-wght-normal.woff2"}
SLUG = re.compile(r"^[a-z2-7]{24}$")
HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-modals allow-popups "
                               "allow-popups-to-escape-sandbox allow-downloads",
    "X-Content-Type-Options": "nosniff",
    "Access-Control-Allow-Origin": "*",   # the opaque origin still has to fetch the page's own files
    "Referrer-Policy": "no-referrer",     # keeps the link out of requests to CDNs and outbound links
    "X-Robots-Tag": "noindex",
    "Cache-Control": "no-cache",          # revalidate, so `--update` shows up on a reload
}


def listing() -> list[dict]:
    """Every artifact, newest first, for the dashboard (which is behind the Tailscale check)."""
    items = []
    for path in ROOT.glob("*/meta.json"):
        try:
            items.append({**json.loads(path.read_text()), "slug": path.parent.name})
        except (OSError, ValueError):
            continue   # being replaced or removed right now
    return sorted(items, key=lambda a: a["updated"], reverse=True)


async def serve(request: web.Request) -> web.StreamResponse:
    slug = request.match_info["slug"]
    if slug == "theme" and request.match_info.get("path") in THEME:
        return web.FileResponse(STATIC / request.match_info["path"], headers=HEADERS)
    if not SLUG.match(slug) or not (ROOT / slug / "site").is_dir():
        raise web.HTTPNotFound(text="no such artifact")
    if "path" not in request.match_info:
        raise web.HTTPPermanentRedirect(f"/a/{slug}/")   # so the page's relative links resolve
    root = (ROOT / slug / "site").resolve()
    target = (root / request.match_info["path"]).resolve()
    if target.is_dir():
        target /= "index.html"
    if not target.is_relative_to(root) or not target.is_file():
        raise web.HTTPNotFound(text="no such file")
    return web.FileResponse(target, headers=HEADERS)
