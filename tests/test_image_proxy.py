from __future__ import annotations

import asyncio

import pytest
from astrbot_plugin_pixiv_lookup.exceptions import ImageDownloadError, MetadataError
from astrbot_plugin_pixiv_lookup.image_proxy import (
    MAX_IMAGE_BYTES,
    PixivImageProxy,
    proxy_order,
    quality_candidates,
    rewrite_image_url,
)
from astrbot_plugin_pixiv_lookup.models import ArtworkPage, DownloadedImage


def test_proxy_order_and_url_rewrite():
    assert proxy_order("i.pixiv.nl") == ("i.pixiv.nl", "i.pixiv.re")
    assert proxy_order("invalid.example") == ("i.pixiv.re", "i.pixiv.nl")
    url = rewrite_image_url("https://i.pximg.net/path/image.jpg?x=1", "i.pixiv.re")
    assert url == "https://i.pixiv.re/path/image.jpg?x=1"
    with pytest.raises(MetadataError):
        rewrite_image_url("https://example.com/image.jpg", "i.pixiv.re")


def test_quality_candidates_only_downgrade():
    page = ArtworkPage(
        1,
        {
            "original": "https://i.pximg.net/o.jpg",
            "large": "https://i.pximg.net/l.jpg",
            "medium": "https://i.pximg.net/m.jpg",
        },
    )
    assert [q for q, _ in quality_candidates(page, "original")] == [
        "original",
        "large",
        "medium",
    ]
    assert [q for q, _ in quality_candidates(page, "medium")] == ["medium"]


@pytest.mark.anyio
async def test_fetch_falls_back_between_proxy_hosts(monkeypatch):
    proxy = PixivImageProxy("i.pixiv.re", 10)
    calls = []

    async def fake_download(url, quality, host):
        calls.append(host)
        if host == "i.pixiv.re":
            raise ImageDownloadError("down")
        return DownloadedImage(b"image", "image/jpeg", quality, host)

    monkeypatch.setattr(proxy, "_download", fake_download)
    image = await proxy.fetch(
        ArtworkPage(1, {"large": "https://i.pximg.net/a.jpg"}),
        "large",
    )
    assert calls == ["i.pixiv.re", "i.pixiv.nl"]
    assert image.proxy_host == "i.pixiv.nl"


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, *, status=200, content_type="image/jpeg", chunks=None, length=None):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = FakeContent(chunks or [b"ok"])
        self.content_length = length

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    closed = False

    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_download_rejects_non_image(monkeypatch):
    proxy = PixivImageProxy("i.pixiv.re", 10)
    response = FakeResponse(content_type="text/html")
    monkeypatch.setattr(proxy, "_get_session", lambda: FakeSession(response))
    with pytest.raises(ImageDownloadError):
        await proxy._download("https://i.pixiv.re/a.jpg", "large", "i.pixiv.re")


@pytest.mark.anyio
async def test_download_rejects_declared_oversize(monkeypatch):
    proxy = PixivImageProxy("i.pixiv.re", 10)
    response = FakeResponse(length=MAX_IMAGE_BYTES + 1)
    monkeypatch.setattr(proxy, "_get_session", lambda: FakeSession(response))
    with pytest.raises(ImageDownloadError):
        await proxy._download("https://i.pixiv.re/a.jpg", "original", "i.pixiv.re")


@pytest.mark.anyio
async def test_original_oversize_downgrades_to_large(monkeypatch):
    proxy = PixivImageProxy("i.pixiv.re", 10)
    calls = []

    async def fake_download(url, quality, host):
        calls.append((quality, host))
        if quality == "original":
            raise ImageDownloadError("too large")
        return DownloadedImage(b"large", "image/jpeg", quality, host)

    monkeypatch.setattr(proxy, "_download", fake_download)
    result = await proxy.fetch(
        ArtworkPage(
            1,
            {
                "original": "https://i.pximg.net/o.jpg",
                "large": "https://i.pximg.net/l.jpg",
            },
        ),
        "original",
    )
    assert calls == [
        ("original", "i.pixiv.re"),
        ("original", "i.pixiv.nl"),
        ("large", "i.pixiv.re"),
    ]
    assert result.quality == "large"


@pytest.mark.anyio
async def test_download_maps_timeout_and_http_error(monkeypatch):
    class TimeoutSession:
        closed = False

        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError

    proxy = PixivImageProxy("i.pixiv.re", 10)
    monkeypatch.setattr(proxy, "_get_session", lambda: TimeoutSession())
    with pytest.raises(ImageDownloadError, match="网络"):
        await proxy._download("https://i.pixiv.re/a.jpg", "large", "i.pixiv.re")

    monkeypatch.setattr(
        proxy,
        "_get_session",
        lambda: FakeSession(FakeResponse(status=502)),
    )
    with pytest.raises(ImageDownloadError, match="502"):
        await proxy._download("https://i.pixiv.re/a.jpg", "large", "i.pixiv.re")


@pytest.mark.anyio
async def test_download_rejects_streamed_oversize_and_close(monkeypatch):
    monkeypatch.setattr("astrbot_plugin_pixiv_lookup.image_proxy.MAX_IMAGE_BYTES", 3)
    session = FakeSession(FakeResponse(chunks=[b"12", b"34"]))
    proxy = PixivImageProxy("i.pixiv.re", 10)
    monkeypatch.setattr(proxy, "_get_session", lambda: session)
    with pytest.raises(ImageDownloadError):
        await proxy._download("https://i.pixiv.re/a.jpg", "large", "i.pixiv.re")
    proxy._session = session
    await proxy.close()
    assert session.closed
