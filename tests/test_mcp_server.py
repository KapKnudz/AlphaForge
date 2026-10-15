"""Unit tests for the read-only Börsdata/MFN MCP server (mocked providers)."""

from __future__ import annotations

import pytest

from alphaforge.mcp import server


class FakeBorsdata:
    api_key = "SECRETKEY123"

    def get_instruments(self):
        return [
            {"insId": 750, "ticker": "EVO", "name": "Evolution", "isin": "SE0012673267"},
            {"insId": 19, "ticker": "ATCO A", "name": "Atlas Copco A", "isin": "SE0017486889"},
        ]

    def get_reports(self, ins_ids, **kwargs):
        return [
            {"insId": 750, "period_type": "year", "report_End_Date": "2025-12-31", "net_Sales": 100},
            {"insId": 750, "period_type": "quarter", "report_End_Date": "2026-06-30"},
        ]

    def get_stock_prices(self, ins_id, *, max_count=None):
        return [{"d": "2026-10-14", "c": 665.2}]

    def get_kpi_history(self, *args):
        return [{"y": 2025, "p": 5, "v": 1.5}]


class FakeMfn:
    def discover_feed(self, slug, *, reports_only=True):
        return [{"url": "https://mfn.se/a/x", "title": "Interim report"}]


@pytest.fixture(autouse=True)
def fake_providers(monkeypatch):
    monkeypatch.setattr(server, "_borsdata", FakeBorsdata())
    monkeypatch.setattr(server, "_mfn", FakeMfn())
    monkeypatch.setattr(server, "_cache", {})
    monkeypatch.setenv("BORSDATA_API_KEY", "SECRETKEY123")
    yield


def _call(fn):
    # FastMCP v1 keeps the wrapped function callable via .fn in some versions.
    return fn.fn if hasattr(fn, "fn") else fn


def test_search_filters_and_envelopes_hash():
    out = _call(server.borsdata_search_company)("evo")
    assert out["source"] == "borsdata/instruments"
    assert out["count"] == 1
    assert out["data"][0]["ticker"] == "EVO"
    assert len(out["sha256"]) == 64
    assert "fetched_at" in out


def test_reports_filter_by_period_type():
    out = _call(server.borsdata_reports)(750, period_type="year")
    assert out["count"] == 1
    assert out["data"][0]["net_Sales"] == 100


def test_prices_and_kpi_history_shapes():
    assert _call(server.borsdata_prices)(750)["data"][0]["c"] == 665.2
    assert _call(server.borsdata_kpi_history)(750, 37)["data"][0]["v"] == 1.5


def test_mfn_reports_shape():
    out = _call(server.mfn_reports)("a/evolution")
    assert out["source"] == "mfn/reports"
    assert out["count"] == 1


def test_provider_error_is_sanitized(monkeypatch):
    class Boom(FakeBorsdata):
        def get_stock_prices(self, ins_id, *, max_count=None):
            raise RuntimeError("GET /v1/x?authKey=SECRETKEY123 failed")

    monkeypatch.setattr(server, "_borsdata", Boom())
    out = _call(server.borsdata_prices)(750)
    assert "SECRETKEY123" not in out["error"]
    assert "***" in out["error"]
