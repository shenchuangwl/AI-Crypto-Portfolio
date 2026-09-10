"""网关 /latest 必须发紧凑 JSON，并可按 Accept-Encoding 预压缩。

indent=2 曾把选币榜Y 快照从 ~2.4MB 撑到 ~3.5MB；浏览器首屏卡在
「加载选币快照」。这套用例把 compact + gzip + 同指纹缓存钉死。
"""

from __future__ import annotations

import gzip
import importlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))
sys.path.insert(0, str(ROOT / "services" / "api-gateway"))


def _load_gateway():
    return importlib.import_module("mock_server")


def _snap(scan_id: str = "20260908-031") -> dict:
    return {
        "meta": {
            "scan_id": scan_id,
            "state_counts": {"CONFIRMED": 1},
            "parameter_version": "param-v2.0.0-screener-y",
        },
        "long_pool": [
            {"symbol": "BTCUSDT", "state": "CONFIRMED", "direction": "up"},
        ],
        "short_pool": [],
        "transitions": [],
    }


def test_dumps_wire_is_compact_not_pretty():
    gw = _load_gateway()
    raw = gw.dumps_wire({"a": 1, "b": [True, None]})
    assert raw == b'{"a":1,"b":[true,null]}'
    assert b"\n" not in raw
    assert b"  " not in raw


def test_gzip_wire_roundtrip():
    gw = _load_gateway()
    raw = gw.dumps_wire({"x": "y" * 200})
    gz = gw.gzip_wire(raw)
    assert gzip.decompress(gz) == raw
    assert len(gz) < len(raw)


def test_latest_wire_compact_gzip_and_fingerprint_cache():
    gw = _load_gateway()
    tmp = Path(tempfile.mkdtemp(prefix="gw-wire-"))
    latest = tmp / "latest.json"
    latest.write_text(json.dumps(_snap(), indent=2), encoding="utf-8")
    orig = gw.board_latest_path
    gw.board_latest_path = lambda key: latest
    gw._SNAP.clear()
    gw._WIRE.clear()
    gw._WIRE_BUILD_LOCKS.clear()
    try:
        wire = gw.latest_wire("y")
        assert wire is not None
        raw, gz = wire
        assert b"\n" not in raw
        body = json.loads(raw)
        assert body["meta"]["board_key"] == "y"
        assert body["meta"]["confirmed_count"] == 1
        assert body["meta"]["api_source"] == str(latest)
        assert gzip.decompress(gz) == raw
        again = gw.latest_wire("y")
        assert again is not None
        assert again[0] is raw
        assert again[1] is gz
    finally:
        gw.board_latest_path = orig
        gw._SNAP.clear()
        gw._WIRE.clear()
        gw._WIRE_BUILD_LOCKS.clear()


def test_latest_meta_is_header_only():
    gw = _load_gateway()
    tmp = Path(tempfile.mkdtemp(prefix="gw-meta-"))
    latest = tmp / "latest.json"
    latest.write_text(json.dumps(_snap("20260909-001"), indent=2), encoding="utf-8")
    orig = gw.board_latest_path
    gw.board_latest_path = lambda key: latest
    gw._SNAP.clear()
    gw._WIRE.clear()
    gw._WIRE_BUILD_LOCKS.clear()
    try:
        body = gw.latest_meta("main")
        assert body is not None
        assert body["scan_id"] == "20260909-001"
        assert body["confirmed_count"] == 1
        assert body["long_count"] == 1
        assert body["short_count"] == 0
        assert "long_pool" not in body
        dumped = json.dumps(body)
        assert len(dumped) < 2000
    finally:
        gw.board_latest_path = orig
        gw._SNAP.clear()
        gw._WIRE.clear()
        gw._WIRE_BUILD_LOCKS.clear()


def test_handler_speaks_http11():
    gw = _load_gateway()
    assert gw.Handler.protocol_version == "HTTP/1.1"


def test_latest_wire_missing_secondary_board_is_none():
    gw = _load_gateway()
    missing = Path(tempfile.mkdtemp(prefix="gw-miss-")) / "nope.json"
    orig = gw.board_latest_path
    gw.board_latest_path = lambda key: missing
    gw._SNAP.clear()
    gw._WIRE.clear()
    gw._WIRE_BUILD_LOCKS.clear()
    try:
        assert gw.latest_wire("y") is None
        snap, source = gw.load_screener_latest("y")
        assert snap == {}
        assert source == "missing"
    finally:
        gw.board_latest_path = orig
        gw._SNAP.clear()
        gw._WIRE.clear()
        gw._WIRE_BUILD_LOCKS.clear()


def test_send_json_bytes_sets_gzip_when_client_accepts():
    gw = _load_gateway()
    raw = gw.dumps_wire({"ok": True, "pad": "z" * 600})
    gz = gw.gzip_wire(raw)
    h = object.__new__(gw.Handler)
    h.headers = {"Accept-Encoding": "gzip, deflate, br"}
    h.wfile = io.BytesIO()
    sent: list[tuple[str, str]] = []
    h.send_response = lambda code: setattr(h, "code", code)
    h.send_header = lambda k, v: sent.append((k, v))
    h.end_headers = lambda: None
    h._cors = lambda: None
    h._send_json_bytes(200, raw, gz)
    headers = dict(sent)
    assert h.code == 200
    assert headers["Content-Encoding"] == "gzip"
    assert headers["Vary"] == "Accept-Encoding"
    assert int(headers["Content-Length"]) == len(gz)
    assert h.wfile.getvalue() == gz


def test_send_json_bytes_plain_without_accept_encoding():
    gw = _load_gateway()
    raw = gw.dumps_wire({"ok": True})
    h = object.__new__(gw.Handler)
    h.headers = {}
    h.wfile = io.BytesIO()
    sent: list[tuple[str, str]] = []
    h.send_response = lambda code: setattr(h, "code", code)
    h.send_header = lambda k, v: sent.append((k, v))
    h.end_headers = lambda: None
    h._cors = lambda: None
    h._send_json_bytes(200, raw)
    headers = dict(sent)
    assert "Content-Encoding" not in headers
    assert h.wfile.getvalue() == raw


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
