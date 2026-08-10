from __future__ import annotations

import sys
import types

import pytest

from could_not_dispatch import media
from could_not_dispatch.constants import KIND_IMAGE, KIND_VIDEO


def test_is_remote_only_for_http_schemes():
    assert media.is_remote("http://example.test/a.png")
    assert media.is_remote("https://example.test/a.png")
    assert not media.is_remote("/data/a.png")
    assert not media.is_remote("file:///data/a.png")


def test_cache_name_keeps_known_suffix_and_is_stable():
    first = media.cache_name("https://example.test/slate.PNG?v=2")
    assert first.endswith(".png")
    assert first == media.cache_name("https://example.test/slate.PNG?v=2")
    assert first != media.cache_name("https://example.test/other.png")


def test_cache_name_falls_back_for_unknown_suffix():
    assert media.cache_name("https://example.test/download?id=7").endswith(".bin")


def test_validate_local_path_accepts_a_file_inside_the_allowed_root(tmp_path):
    target = tmp_path / "slate.png"
    target.write_bytes(b"x")
    assert media.validate_local_path(str(target), [tmp_path]) == target.resolve()


def test_validate_local_path_rejects_a_path_outside_the_allowed_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = outside / "slate.png"
    target.write_bytes(b"x")
    with pytest.raises(media.MediaError, match="outside the allowed"):
        media.validate_local_path(str(target), [allowed])


def test_validate_local_path_rejects_traversal_out_of_the_allowed_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    escape = tmp_path / "secret.png"
    escape.write_bytes(b"x")
    with pytest.raises(media.MediaError, match="outside the allowed"):
        media.validate_local_path(str(allowed / ".." / "secret.png"), [allowed])


def test_validate_local_path_rejects_a_relative_path(tmp_path):
    with pytest.raises(media.MediaError, match="absolute"):
        media.validate_local_path("slate.png", [tmp_path])


def test_validate_local_path_rejects_a_directory(tmp_path):
    with pytest.raises(media.MediaError, match="not a file"):
        media.validate_local_path(str(tmp_path), [tmp_path])


def test_resolve_rejects_an_empty_source(tmp_path):
    with pytest.raises(media.MediaError, match="No image or video"):
        media.resolve("   ", tmp_path)


class _FakeResponse:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield from self._chunks


def _install_fake_requests(monkeypatch, handler):
    module = types.SimpleNamespace(get=lambda url, **kwargs: handler(url, **kwargs))
    monkeypatch.setitem(sys.modules, "requests", module)


def test_download_writes_the_body_and_leaves_no_partial_file(monkeypatch, tmp_path):
    _install_fake_requests(monkeypatch, lambda url, **kw: _FakeResponse([b"abc", b"de"]))
    resolved = media.download("https://example.test/slate.png", tmp_path)
    assert resolved.path.read_bytes() == b"abcde"
    assert resolved.from_cache is False
    assert list(tmp_path.glob("*.part")) == []


def test_download_names_itself_because_image_hosts_refuse_python_requests(
    monkeypatch, tmp_path
):
    seen: dict = {}

    def capture(url, **kwargs):
        seen.update(kwargs)
        return _FakeResponse([b"x"])

    _install_fake_requests(monkeypatch, capture)
    media.download("https://example.test/slate.png", tmp_path)
    agent = seen["headers"]["User-Agent"]
    assert agent.startswith("could-not-dispatch/")
    assert "python-requests" not in agent


def test_download_refuses_a_body_over_the_limit(monkeypatch, tmp_path):
    _install_fake_requests(monkeypatch, lambda url, **kw: _FakeResponse([b"x" * 64]))
    with pytest.raises(media.MediaError, match="larger than"):
        media.download("https://example.test/slate.png", tmp_path, max_bytes=8)
    assert list(tmp_path.glob("*.part")) == []


def test_download_refuses_an_empty_body(monkeypatch, tmp_path):
    _install_fake_requests(monkeypatch, lambda url, **kw: _FakeResponse([]))
    with pytest.raises(media.MediaError, match="empty body"):
        media.download("https://example.test/slate.png", tmp_path)


def test_download_falls_back_to_the_cached_copy_when_the_request_fails(
    monkeypatch, tmp_path
):
    url = "https://example.test/slate.png"
    _install_fake_requests(monkeypatch, lambda u, **kw: _FakeResponse([b"cached"]))
    first = media.download(url, tmp_path)

    def explode(u, **kwargs):
        raise OSError("network down")

    _install_fake_requests(monkeypatch, explode)
    second = media.download(url, tmp_path)
    assert second.path == first.path
    assert second.from_cache is True
    assert second.path.read_bytes() == b"cached"


def test_download_reports_the_failure_when_no_cache_exists(monkeypatch, tmp_path):
    def explode(url, **kwargs):
        raise OSError("network down")

    _install_fake_requests(monkeypatch, explode)
    with pytest.raises(media.MediaError, match="Could not download"):
        media.download("https://example.test/slate.png", tmp_path)


def test_download_overwrites_the_cache_on_a_later_success(monkeypatch, tmp_path):
    url = "https://example.test/slate.png"
    _install_fake_requests(monkeypatch, lambda u, **kw: _FakeResponse([b"old"]))
    media.download(url, tmp_path)
    _install_fake_requests(monkeypatch, lambda u, **kw: _FakeResponse([b"new"]))
    resolved = media.download(url, tmp_path)
    assert resolved.path.read_bytes() == b"new"
    assert resolved.from_cache is False


def test_classify_reads_a_still_image_from_the_container_format():
    probe = {
        "format": {"format_name": "png_pipe"},
        "streams": [{"codec_type": "video", "codec_name": "png"}],
    }
    assert media.classify(probe) == (KIND_IMAGE, False)


def test_classify_reads_a_single_frame_without_audio_as_an_image():
    probe = {
        "format": {"format_name": "mov,mp4,m4a"},
        "streams": [{"codec_type": "video", "nb_frames": "1"}],
    }
    assert media.classify(probe) == (KIND_IMAGE, False)


def test_classify_reads_a_video_and_reports_its_audio():
    probe = {
        "format": {"format_name": "mov,mp4,m4a"},
        "streams": [
            {"codec_type": "video", "nb_frames": "900"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    assert media.classify(probe) == (KIND_VIDEO, True)


def test_classify_rejects_a_file_without_a_video_track():
    probe = {"format": {"format_name": "mp3"}, "streams": [{"codec_type": "audio"}]}
    with pytest.raises(media.MediaError, match="no video"):
        media.classify(probe)
