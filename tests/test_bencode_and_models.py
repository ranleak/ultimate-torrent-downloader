"""Tests for bencode parser, torrent metadata inspector, magnet parsing, and models."""

from __future__ import annotations

import hashlib

import pytest

from utd.shared.bencode import BencodeError, bdecode, bencode, inspect_torrent_bytes
from utd.shared.models import (
    MagnetSubmitRequest,
    TorrentOptions,
    parse_magnet_info,
    sanitize_volume_path,
    sanitize_volume_subpath,
    validate_speed_limit,
)
from utd.shared.trackers import merged_trackers_csv


def test_bencode_roundtrip_and_canonical_dict_order() -> None:
    payload = {
        b"z": [1, -42, b"hello", b""],
        b"a": {b"nested": b"value"},
    }
    encoded = bencode(payload)
    assert encoded.startswith(b"d1:ad")
    assert bdecode(encoded) == payload


@pytest.mark.parametrize(
    "bad",
    [b"", b"i", b"i01e", b"i-0e", b"l1:a", b"d1:a", b"5:abc", b"1:a1:b"],
)
def test_bencode_rejects_malformed(bad: bytes) -> None:
    with pytest.raises(BencodeError):
        bdecode(bad)


def test_inspect_single_and_multi_file_torrent() -> None:
    single_info = {
        b"name": b"ubuntu.iso",
        b"length": 1024 * 1024,
        b"piece length": 262144,
        b"pieces": b"0" * 20,
    }
    single_raw = bencode(
        {
            b"announce": b"https://tracker.example/announce",
            b"comment": b"Official ISO",
            b"info": single_info,
        }
    )
    meta = inspect_torrent_bytes(single_raw)
    assert meta.name == "ubuntu.iso"
    assert meta.info_hash == hashlib.sha1(bencode(single_info)).hexdigest()
    assert meta.total_size == 1024 * 1024
    assert len(meta.files) == 1
    assert meta.files[0].path == "ubuntu.iso"
    assert meta.trackers == ["https://tracker.example/announce"]
    assert meta.is_private is False
    assert meta.comment == "Official ISO"

    multi_info = {
        b"name.utf-8": "My Album \u2605".encode(),
        b"private": 1,
        b"piece length": 262144,
        b"pieces": b"1" * 20,
        b"files": [
            {b"length": 100, b"path": [b"01.flac"]},
            {b"length": 250, b"path": [b"extras", b"cover.jpg"]},
        ],
    }
    multi_raw = bencode(
        {
            b"announce": b"https://priv.tracker/ann",
            b"announce-list": [[b"https://priv.tracker/ann"], [b"https://priv2.tracker/ann"]],
            b"info": multi_info,
        }
    )
    m2 = inspect_torrent_bytes(multi_raw)
    assert m2.name == "My Album \u2605"
    assert m2.is_private is True
    assert m2.total_size == 350
    assert [f.index for f in m2.files] == [1, 2]
    assert m2.files[1].path == "My Album \u2605/extras/cover.jpg"
    assert m2.trackers == ["https://priv.tracker/ann", "https://priv2.tracker/ann"]


def test_magnet_parsing_and_validation() -> None:
    uri = (
        "magnet:?xt=urn:btih:ABCDEF1234567890ABCDEF1234567890ABCDEF12"
        "&dn=Debian%2012&tr=https%3A%2F%2Ft.org%2Fann"
    )
    parsed = parse_magnet_info(uri)
    assert parsed["info_hash"] == "abcdef1234567890abcdef1234567890abcdef12"
    assert parsed["name"] == "Debian 12"
    assert parsed["trackers"] == ["https://t.org/ann"]

    req = MagnetSubmitRequest(uri=uri)
    assert req.uri == uri

    with pytest.raises(ValueError):
        MagnetSubmitRequest(uri="magnet:?dn=nohash")

    with pytest.raises(ValueError):
        MagnetSubmitRequest(uri="ftp://unsupported.example/file.iso")


def test_path_and_speed_sanitization() -> None:
    assert sanitize_volume_subpath("/movies\\2026/") == "movies/2026"
    assert sanitize_volume_path("/") == ""
    assert sanitize_volume_path("/downloads/sub/file.bin") == "downloads/sub/file.bin"

    with pytest.raises(ValueError):
        sanitize_volume_subpath("../escape")
    with pytest.raises(ValueError):
        sanitize_volume_path("downloads/../../etc/passwd")
    with pytest.raises(ValueError):
        sanitize_volume_subpath("bad:name")

    assert validate_speed_limit(" 50M ") == "50M"
    assert validate_speed_limit("") == "0"
    with pytest.raises(ValueError):
        validate_speed_limit("fast")

    opts = TorrentOptions(selected_files=[3, 1, 3])
    assert opts.selected_files == [1, 3]


def test_tracker_merging_respects_private_torrents() -> None:
    assert merged_trackers_csv(["https://a.example/ann"], is_private=True) == ""
    csv = merged_trackers_csv(["https://custom.example/ann"], is_private=False)
    assert csv.startswith("https://custom.example/ann,")
    assert "tracker.opentrackr.org" in csv
