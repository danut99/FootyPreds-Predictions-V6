"""GET /api/img: strict allowlist, redirects, size/type/timeout limits and the disk cache.

Every upstream answer comes from an httpx.MockTransport; nothing reaches the network.
"""

import asyncio
import re
import struct
import zlib
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds import media
from footypreds.api import create_app
from footypreds.config import Settings

CREST = "https://static.flashscore.com/res/image/data/Q1Fx8AcM-bqw7ByeG.png"
FLAG = "https://flagcdn.com/w40/ar.png"


def tiny_png():
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


PNG = tiny_png()


class Upstream:
    """Fake image hosts: `routes` maps a full URL to a handler(request) -> httpx.Response."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def __call__(self, request):
        url = str(request.url)
        self.calls.append(url)
        handler = self.routes.get(url)
        if handler is None:
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return handler(request)

    def hosts(self):
        return {httpx.URL(url).host for url in self.calls}


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def app(tmp_path, upstream):
    settings = Settings(api_key="k", database=tmp_path / "db" / "img.db")
    application = create_app(
        settings,
        httpx.MockTransport(lambda request: httpx.Response(500)),
        httpx.MockTransport(upstream),
    )
    application.state.img_cache_dir = tmp_path / "cache"
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def get(client, url, **headers):
    return client.get("/api/img?u=" + quote(url, safe=""), headers=headers)


def cached_files(app):
    folder = app.state.img_cache_dir
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_allowed_image_is_proxied_cached_on_disk_and_served_again_without_upstream(
    client, app, upstream
):
    first = get(client, CREST)
    assert first.status_code == 200
    assert first.content == PNG
    assert first.headers["content-type"] == "image/png"
    assert first.headers["cache-control"] == "public, max-age=604800"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert "img-src 'self' data:;" in first.headers["content-security-policy"]
    files = cached_files(app)
    assert files == [media.cache_name(CREST)]
    assert re.fullmatch(r"[0-9a-f]{64}\.img", files[0])
    again = get(client, CREST)
    assert again.status_code == 200 and again.content == PNG
    assert upstream.calls == [CREST]
    assert get(client, FLAG).status_code == 200
    assert upstream.calls == [CREST, FLAG]


def test_display_urls_from_responses_work_as_is(client):
    assert client.get(media.display_url(FLAG)).status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/a.png",
        "http://static.flashscore.com/res/image/data/a.png",
        "https://static.flashscore.com@evil.com/a.png",
        "https://user@static.flashscore.com/a.png",
        "https://static.flashscore.com.evil.com/a.png",
        "https://static.flashscore.com:444/a.png",
        "https://127.0.0.1/a.png",
        "https://localhost/a.png",
        "https://169.254.169.254/latest/meta-data",
        "file:///C:/Windows/win.ini",
        "https://static.flashscore.com/../../footypreds.sqlite3",
        "https://static.flashscore.com/res/%2e%2e/%2e%2e/a.png",
        "https://static.flashscore.com/a.png?x=../../b",
        "",
    ],
)
def test_disallowed_urls_are_404_without_any_upstream_request(client, app, upstream, url):
    response = get(client, url)
    assert response.status_code == 404
    assert response.json() == {"detail": media.NOT_FOUND}
    assert upstream.calls == []
    assert cached_files(app) == []


def test_missing_and_overlong_parameters_are_404(client, upstream):
    assert client.get("/api/img").status_code == 404
    assert client.get("/api/img?u=" + "a" * 5000).status_code == 404
    assert get(client, CREST[:-4] + "a" * 400 + ".png").status_code == 404
    assert upstream.calls == []


def test_redirect_to_another_host_is_never_followed(client, app, upstream):
    upstream.routes[CREST] = lambda r: httpx.Response(
        302, headers={"location": "https://evil.com/x.png"}
    )
    assert get(client, CREST).status_code == 404
    assert upstream.hosts() == {"static.flashscore.com"}
    assert cached_files(app) == []


@pytest.mark.parametrize(
    "location",
    [
        "http://static.flashscore.com/res/image/data/b.png",
        "//evil.com/x.png",
        "https://static.flashscore.com@evil.com/x.png",
        "https://flagcdn.com.evil.com/x.png",
        "https://static.flashscore.com/a.png?next=https://evil.com",
        "Q1Fx8AcM-bqw7ByeG.png",  # to itself
        "",
    ],
)
def test_redirects_to_unsafe_targets_are_refused(client, upstream, location):
    upstream.routes[CREST] = lambda r: httpx.Response(301, headers={"location": location})
    assert get(client, CREST).status_code == 404
    assert upstream.calls == [CREST]


def test_redirect_within_the_allowed_hosts_is_followed(client, upstream):
    target = "https://static.flashscore.com/res/image/data/moved.png"
    upstream.routes[CREST] = lambda r: httpx.Response(302, headers={"location": "moved.png"})
    response = get(client, CREST)
    assert response.status_code == 200 and response.content == PNG
    assert upstream.calls == [CREST, target]


def test_dot_segments_in_a_redirect_resolve_on_the_same_allowed_host(client, upstream):
    upstream.routes[CREST] = lambda r: httpx.Response(
        302, headers={"location": "/../../etc/passwd.png"}
    )
    assert get(client, CREST).status_code == 200
    assert upstream.calls == [CREST, "https://static.flashscore.com/etc/passwd.png"]


def test_redirect_loops_stop(client, upstream):
    other = "https://flagcdn.com/w40/es.png"
    upstream.routes[CREST] = lambda r: httpx.Response(302, headers={"location": other})
    upstream.routes[other] = lambda r: httpx.Response(302, headers={"location": CREST})
    assert get(client, CREST).status_code == 404
    assert len(upstream.calls) == media.MAX_REDIRECTS + 1


def test_oversized_images_are_refused(client, app, upstream):
    big = PNG + b"\x00" * (media.MAX_BYTES + 1)
    upstream.routes[CREST] = lambda r: httpx.Response(
        200, content=big, headers={"content-type": "image/png"}
    )
    assert get(client, CREST).status_code == 404

    async def stream():
        yield PNG
        for _ in range(9):
            yield b"\x00" * 65536

    # No Content-Length: the body is cut as soon as it passes the limit.
    upstream.routes[FLAG] = lambda r: httpx.Response(
        200, content=stream(), headers={"content-type": "image/png"}
    )
    assert get(client, FLAG).status_code == 404
    assert cached_files(app) == []


def test_exactly_the_limit_is_accepted(client, upstream):
    body = PNG + b"\x00" * (media.MAX_BYTES - len(PNG))
    upstream.routes[CREST] = lambda r: httpx.Response(
        200, content=body, headers={"content-type": "image/png"}
    )
    assert get(client, CREST).status_code == 200


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"<html><script>alert(1)</script></html>", "text/html"),
        (
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "image/svg+xml",
        ),
        (b"<html>not an image</html>", "image/png"),  # declared png, sniffed html
        (PNG, "text/plain"),  # real png bytes, wrong declared type
        (PNG, ""),
        (b"GIF89a" + b"\x00" * 10, "image/png;charset=x"),  # GIF bytes are fine: type sniffed
    ],
)
def test_only_real_raster_images_are_served(client, upstream, content, content_type):
    headers = {"content-type": content_type} if content_type else {}
    upstream.routes[CREST] = lambda r: httpx.Response(200, content=content, headers=headers)
    response = get(client, CREST)
    if content.startswith(b"GIF89a"):
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/gif"
    else:
        assert response.status_code == 404


@pytest.mark.parametrize("status", [204, 304, 403, 404, 500, 503])
def test_upstream_errors_are_404_and_remembered_for_a_while(client, upstream, status):
    upstream.routes[CREST] = lambda r: httpx.Response(status)
    assert get(client, CREST).status_code == 404
    assert get(client, CREST).status_code == 404
    assert upstream.calls == [CREST]  # the failure is not retried at once


def test_a_failure_is_retried_after_the_failure_window(app, client, upstream, monkeypatch):
    upstream.routes[CREST] = lambda r: httpx.Response(500)
    assert get(client, CREST).status_code == 404
    proxy = app.state.img_proxy
    proxy.failures[CREST] = proxy.clock() - 1
    del upstream.routes[CREST]
    assert get(client, CREST).status_code == 200
    assert upstream.calls == [CREST, CREST]


def test_network_errors_and_timeouts_are_404(client, upstream, monkeypatch):
    def broken(request):
        raise httpx.ConnectError("down", request=request)

    upstream.routes[CREST] = broken
    assert get(client, CREST).status_code == 404

    def read_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    upstream.routes[FLAG] = read_timeout
    assert get(client, FLAG).status_code == 404


def test_the_whole_download_has_a_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "TIMEOUT", 0.05)

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async def run():
        proxy = media.ImageProxy(tmp_path / "c", httpx.MockTransport(slow))
        try:
            return await proxy.get(CREST)
        finally:
            await proxy.close()

    assert asyncio.run(run()) is None
    assert not (tmp_path / "c").exists()


def test_concurrent_requests_for_one_image_fetch_it_once(tmp_path):
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        await asyncio.sleep(0.02)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async def run():
        proxy = media.ImageProxy(tmp_path / "c", httpx.MockTransport(handler))
        try:
            return await asyncio.gather(*(proxy.get(CREST) for _ in range(5)))
        finally:
            await proxy.close()

    results = asyncio.run(run())
    assert all(r == (PNG, "image/png") for r in results)
    assert calls == [CREST]


def test_a_corrupted_cache_file_is_fetched_again(client, app, upstream):
    assert get(client, CREST).status_code == 200
    path = app.state.img_cache_dir / media.cache_name(CREST)
    path.write_bytes(b"<html>tampered</html>")
    response = get(client, CREST)
    assert response.status_code == 200 and response.content == PNG
    assert upstream.calls == [CREST, CREST]
    assert path.read_bytes() == PNG


def test_cache_files_never_leave_the_cache_folder(client, app, tmp_path):
    for url in (CREST, FLAG, "https://static.flashscore.com/res/image/data/..%2f..%2fx.png"):
        get(client, url)
    names = cached_files(app)
    assert names and all(re.fullmatch(r"[0-9a-f]{64}\.img", n) for n in names)
    outside = [p for p in tmp_path.rglob("*") if p.is_file() and "cache" not in p.parts]
    assert all(p.suffix != ".img" for p in outside)


def test_default_cache_folder_sits_next_to_the_database(tmp_path):
    settings = Settings(api_key="k", database=tmp_path / "data" / "x.db")
    app = create_app(settings)
    assert app.state.img_cache_dir == tmp_path / "data" / "img_cache"
    assert media.proxy_of(app).cache_dir == tmp_path / "data" / "img_cache"


def test_cross_site_image_requests_are_refused(client, upstream):
    response = get(client, CREST, **{"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    assert upstream.calls == []
