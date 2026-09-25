"""Team crests, league logos and player flags (docs/CONTRACTS.md §15).

`Match.home_logo`, `away_logo` and `league_logo` hold the ORIGINAL upstream URLs, already
restricted to `domain.IMAGE_HOSTS`. Every API response shows them as same-origin display URLs,
"/api/img?u=<urlencoded upstream url>" or null, so the page's CSP keeps `img-src 'self' data:`.

GET /api/img?u=... is a strict image proxy:
- the URL must pass `domain.image_url` (https, exact allowed host, simple path, no query);
- a redirect is followed only when its target passes the same check (at most 3 hops);
- the answer must be HTTP 200, `image/png|jpeg|gif|webp` (declared AND sniffed from the bytes;
  never SVG), at most 512 KB, within 10 s;
- good images are cached on disk under `<data>/img_cache/<sha256>.img` (hash filenames only,
  so no path can be influenced by the client) and served with a one-week Cache-Control;
- anything else is a 404: the UI then shows an initials badge. Failures are remembered for a
  few minutes so a broken crest does not reach upstream on every page view.

Tests and the mock server set `app.state.img_transport` (an httpx transport) and
`app.state.img_cache_dir`; production uses the network and `<database dir>/img_cache`.
"""

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from footypreds.domain import IMAGE_URL_MAX, image_url

router = APIRouter(prefix="/api", tags=["media"])
log = logging.getLogger(__name__)

DISPLAY_PREFIX = "/api/img?u="
MEDIA_FIELDS = ("home_logo", "away_logo", "league_logo")
MAX_BYTES = 512 * 1024
TIMEOUT = 10.0
MAX_REDIRECTS = 3
CACHE_CONTROL = "public, max-age=604800"
DISK_TTL = 30 * 86400  # crests change rarely; refetch a month later
FAILURE_TTL = 600  # seconds a failed URL is not retried
CONCURRENCY = 6
CONTENT_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
NOT_FOUND = "Imaginea nu este disponibilă."


# --- display URLs -------------------------------------------------------------------------


def display_url(url):
    """Same-origin URL that shows `url` through the proxy, or None when it is not allowed."""
    url = image_url(url)
    return DISPLAY_PREFIX + quote(url, safe="") if url else None


def match_media(match):
    """{"home_logo", "away_logo", "league_logo"} display URLs of a Match (None when absent)."""
    return {field: display_url(getattr(match, field, None)) for field in MEDIA_FIELDS}


def public_match(match):
    """A Match as JSON for an API response: upstream logo URLs replaced by display URLs."""
    return match.model_dump(mode="json") | match_media(match)


def is_leg(item):
    return isinstance(item, dict) and {"match_id", "home", "away", "key"} <= item.keys()


def with_leg_media(payload, store):
    """`payload` with display logos on every leg that lacks them (legs stored earlier).

    Walks dicts and lists; a leg is any dict with match_id, home, away and key. The logos come
    from the stored match (None when unknown). Legs that already carry the fields are kept.
    """
    known = {}

    def media_of(match_id):
        if match_id not in known:
            match = store.match(match_id) if isinstance(match_id, str) and match_id else None
            known[match_id] = match_media(match) if match else dict.fromkeys(MEDIA_FIELDS)
        return known[match_id]

    def visit(item):
        if isinstance(item, list):
            return [visit(value) for value in item]
        if not isinstance(item, dict):
            return item
        output = {key: visit(value) for key, value in item.items()}
        if is_leg(output) and not all(field in output for field in MEDIA_FIELDS):
            output = {**media_of(output["match_id"]), **output}
        return output

    return visit(payload)


# --- proxy ---------------------------------------------------------------------------------


def sniff(content):
    """Image type from the first bytes, or None (SVG and anything else are refused)."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def cache_name(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest() + ".img"


class ImageProxy:
    """Fetches allowed images once, keeps them on disk and serves them from there."""

    def __init__(self, cache_dir, transport=None, clock=time.monotonic):
        self.cache_dir = Path(cache_dir)
        self.transport = transport
        self.clock = clock
        self.client = None
        self.failures = {}
        self.inflight = {}
        self.semaphore = None
        self.loop = None

    def bind(self):
        """Loop-bound helpers are rebuilt when the event loop changes (TestClient portals)."""
        loop = asyncio.get_running_loop()
        if loop is not self.loop:
            self.loop = loop
            self.semaphore = asyncio.Semaphore(CONCURRENCY)
            self.client = None
            self.inflight = {}

    def path_of(self, url):
        # Only a hex digest reaches the filesystem: the client cannot choose the path.
        return self.cache_dir / cache_name(url)

    def read_cached(self, url):
        path = self.path_of(url)
        try:
            if time.time() - path.stat().st_mtime > DISK_TTL:
                return None
            content = path.read_bytes()
        except OSError:
            return None
        kind = sniff(content)
        if kind is None or len(content) > MAX_BYTES:
            path.unlink(missing_ok=True)
            return None
        return content, kind

    def write_cached(self, url, content):
        path = self.path_of(url)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(content)
            temporary.replace(path)
        except OSError:
            log.warning("Image cache write failed", exc_info=True)

    def failed_recently(self, url):
        until = self.failures.get(url)
        if until is None:
            return False
        if until <= self.clock():
            self.failures.pop(url, None)
            return False
        return True

    def remember_failure(self, url):
        if len(self.failures) > 2000:
            now = self.clock()
            self.failures = {k: v for k, v in self.failures.items() if v > now}
        self.failures[url] = self.clock() + FAILURE_TTL

    async def get(self, url):
        """(bytes, content type) of an allowed image, or None."""
        url = image_url(url)
        if url is None:
            return None
        cached = self.read_cached(url)
        if cached is not None:
            return cached
        if self.failed_recently(url):
            return None
        self.bind()
        task = self.inflight.get(url)
        if task is None:
            task = asyncio.ensure_future(self._fetch_and_store(url))
            self.inflight[url] = task
            task.add_done_callback(lambda _: self.inflight.pop(url, None))
        return await asyncio.shield(task)

    async def _fetch_and_store(self, url):
        try:
            async with self.semaphore:
                found = await asyncio.wait_for(self.fetch(url), TIMEOUT)
        except Exception:  # any upstream trouble is just "no image" (CancelledError propagates)
            log.info("Image fetch failed", exc_info=True)
            found = None
        if found is None:
            self.remember_failure(url)
            return None
        self.write_cached(url, found[0])
        return found

    def http(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(TIMEOUT),
                follow_redirects=False,
                transport=self.transport,
                headers={"accept": ", ".join(CONTENT_TYPES), "user-agent": "FootyPreds/8"},
            )
        return self.client

    async def fetch(self, url):
        """Download one image; redirects only to allowed URLs; None when refused."""
        client = self.http()
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    # Any 3xx: follow only a real Location that resolves to another allowed URL.
                    target = response.headers.get("location", "").strip()
                    if not response.has_redirect_location or not target:
                        return None
                    if len(target) > IMAGE_URL_MAX:
                        return None
                    following = image_url(urljoin(url, target))
                    if following is None or following == url:
                        return None
                    url = following
                    continue
                if response.status_code != 200:
                    return None
                declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if declared not in CONTENT_TYPES:
                    return None
                try:
                    length = int(response.headers.get("content-length", "0"))
                except ValueError:
                    return None
                if length > MAX_BYTES:
                    return None
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        return None
            kind = sniff(bytes(content))
            if kind is None:
                return None
            return bytes(content), kind
        return None

    async def close(self):
        for task in list(self.inflight.values()):
            task.cancel()
        if self.client is not None and self.loop is asyncio.get_running_loop():
            await self.client.aclose()
        self.client = None


def proxy_of(app):
    """The app's ImageProxy, created on first use from app.state settings."""
    found = getattr(app.state, "img_proxy", None)
    if found is None:
        cache_dir = getattr(app.state, "img_cache_dir", None)
        if cache_dir is None:
            settings = getattr(app.state, "settings", None)
            base = Path(settings.database).parent if settings else Path("data")
            cache_dir = base / "img_cache"
        found = ImageProxy(cache_dir, getattr(app.state, "img_transport", None))
        app.state.img_proxy = found
    return found


async def close(app):
    found = getattr(app.state, "img_proxy", None)
    if found is not None:
        await found.close()
        app.state.img_proxy = None


@router.get("/img")
async def image(request: Request, u: str = ""):
    # Every refusal is a plain 404, so the page falls back to its initials badge.
    url = image_url(u) if len(u) <= IMAGE_URL_MAX else None
    if url is None:
        raise HTTPException(404, NOT_FOUND)
    found = await proxy_of(request.app).get(url)
    if found is None:
        raise HTTPException(404, NOT_FOUND)
    content, kind = found
    return Response(content, media_type=kind, headers={"Cache-Control": CACHE_CONTROL})
