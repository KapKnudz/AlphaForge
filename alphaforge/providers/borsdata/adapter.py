"""BorsdataAdapter — single adapter implementing MarketDataProvider.

Behavior rules (plan §1.2):
- MAX_RETRIES=3 with Retry-After, batch 50, 0.5-1s sleep, original=0
- maxCount never trusted — fetch full and slice locally
- sector-KPI 400 swallowed as missing
"""

from __future__ import annotations

import os
from typing import Any

import requests

from alphaforge.providers.http import MAX_RETRIES, request_with_retry, sleep_between_batches

BASE_URL = "https://apiservice.borsdata.se"
BATCH_SIZE = 50


class BorsdataAdapter:
    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL) -> None:
        key = api_key or os.environ.get("BORSDATA_API_KEY", "")
        if not key:
            # Allow offline instantiation for tests that mock _get
            key = ""
        self.api_key = key
        self.base_url = base_url.rstrip("/")

    # ---- internal ----

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        url = f"{self.base_url}{path}"
        p: dict[str, Any] = dict(params or {})
        if self.api_key:
            p["authKey"] = self.api_key
        return request_with_retry("GET", url, params=p, max_retries=MAX_RETRIES)

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        resp = self._get(path, params=params)
        # 400 for sector-KPI mismatch → swallowed as missing (return None)
        if resp.status_code == 400:
            return None
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return None

    def _unwrap_list(self, data: Any) -> list[dict[str, Any]]:
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Börsdata wraps in {"instruments": [...]} or {"reports": [...]}, etc.
            for key in (
                "instruments",
                "reports",
                "markets",
                "branches",
                "sectors",
                "countries",
                "kpis",
                "stockPricesList",
                "dividends",
                "insider",
                "buyback",
                "shorts",
                "stockSplits",
                "reportCalendar",
                "translationMetadata",
                "kpiMetadata",
                "reportMetadata",
            ):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # single object wrapped
            if "instrument" in data:
                return [data]
        return []

    # ---- reference dictionaries (should-adds) ----

    def get_instruments(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments")
        return self._unwrap_list(data)

    def get_markets(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/markets")
        return self._unwrap_list(data)

    def get_branches(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/branches")
        if data is None:
            return []
        if isinstance(data, dict) and "branches" in data:
            return data["branches"]
        return self._unwrap_list(data)

    def get_sectors(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/sectors")
        if data is None:
            return []
        if isinstance(data, dict) and "sectors" in data:
            return data["sectors"]
        return self._unwrap_list(data)

    def get_countries(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/countries")
        if data is None:
            return []
        if isinstance(data, dict) and "countries" in data:
            return data["countries"]
        return self._unwrap_list(data)

    def get_translation_metadata(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/translationmetadata")
        return self._unwrap_list(data)

    def get_kpi_metadata(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments/kpis/metadata")
        if data is None:
            return []
        # response may be {"kpis": [...]} or list
        if isinstance(data, dict) and "kpis" in data:
            return data["kpis"]
        return self._unwrap_list(data)

    def get_report_metadata(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments/reports/metadata")
        if data is None:
            return []
        if isinstance(data, dict) and "reports" in data:
            return data["reports"]
        return self._unwrap_list(data)

    # ---- KPIs ----

    def get_kpis(
        self, ins_id: int, kpi_id: int, calc_group: str, calc: str
    ) -> dict[str, Any] | None:
        # GET /v1/instruments/{id}/kpis/{kpiId}/{calcGroup}/{calc}
        path = f"/v1/instruments/{ins_id}/kpis/{kpi_id}/{calc_group}/{calc}"
        data = self._get_json(path)
        if data is None:
            return None  # 400 swallowed
        if isinstance(data, dict):
            return data
        return None

    def get_kpi_history(
        self, ins_id: int, kpi_id: int, report_type: str, price_type: str
    ) -> list[dict[str, Any]]:
        # GET /v1/instruments/{id}/kpis/{kpiId}/{reportType}/{priceType}/history
        path = f"/v1/instruments/{ins_id}/kpis/{kpi_id}/{report_type}/{price_type}/history"
        data = self._get_json(path)
        if data is None:
            return []  # 400 → missing
        if isinstance(data, dict):
            # may be {"values": [...]} or list
            if "values" in data and isinstance(data["values"], list):
                return data["values"]
            if "kpiHistory" in data and isinstance(data["kpiHistory"], list):
                return data["kpiHistory"]
        if isinstance(data, list):
            return data
        return []

    def get_kpi_summary(self, ins_id: int, report_type: str) -> dict[str, Any] | None:
        # GET /v1/instruments/{insid}/kpis/{reporttype}/summary
        path = f"/v1/instruments/{ins_id}/kpis/{report_type}/summary"
        data = self._get_json(path)
        if data is None or isinstance(data, list) and len(data) == 0:
            return None
        if isinstance(data, dict):
            return data
        return None

    # ---- reports (batch ≤50, original=0) ----

    def get_reports(self, ins_ids: list[int], *, original: int = 0) -> list[dict[str, Any]]:
        """Fetch reports for up to N instruments, batch 50, original=0."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(ins_ids), BATCH_SIZE):
            chunk = ins_ids[i : i + BATCH_SIZE]
            params: dict[str, Any] = {
                "instList": ",".join(str(x) for x in chunk),
                "original": original,
            }
            data = self._get_json("/v1/instruments/reports", params=params)
            out.extend(self._unwrap_list(data))
            if i + BATCH_SIZE < len(ins_ids):
                sleep_between_batches()
        return out

    def get_report_calendar(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments/report/calendar")
        return self._unwrap_list(data)

    def get_stock_splits(self, *, from_date: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        data = self._get_json("/v1/instruments/StockSplits", params=params or None)
        return self._unwrap_list(data)

    # ---- prices (maxCount not trusted) ----

    def get_stock_prices(
        self, ins_id: int, *, max_count: int | None = None
    ) -> list[dict[str, Any]]:
        # Always fetch full; slice locally if max_count given.
        # Plan: maxCount is ignored by backend — fetch full then slice locally.
        params: dict[str, Any] = {}
        # Do NOT send maxCount; fetch full.
        data = self._get_json(f"/v1/instruments/{ins_id}/stockprices", params=params or None)
        rows: list[dict[str, Any]] = []
        if isinstance(data, dict):
            if "stockPricesList" in data and isinstance(data["stockPricesList"], list):
                rows = data["stockPricesList"]
            elif "stockPrices" in data and isinstance(data["stockPrices"], list):
                rows = data["stockPrices"]
            elif isinstance(data, list):
                rows = data  # type: ignore[assignment]
        elif isinstance(data, list):
            rows = data
        else:
            rows = self._unwrap_list(data)
        # Sort by date (n ascending = oldest first as per API), then slice tail if requested
        # Börsdata returns 10y default sorted asc; we keep order and slice locally.
        if max_count is not None and len(rows) > max_count:
            rows = rows[-max_count:]
        return rows

    # ---- dividends (zero-row dropped at adapter) ----

    def get_dividends(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments/dividend/calendar")
        rows = self._unwrap_list(data)
        # Drop zero-row: amountPaid 0.0 with currency = explicit "no distribution"
        filtered: list[dict[str, Any]] = []
        for r in rows:
            amt = r.get("amountPaid") if "amountPaid" in r else r.get("amount")
            try:
                if amt is not None and float(amt) == 0.0:
                    continue
            except (TypeError, ValueError):
                pass
            filtered.append(r)
        return filtered

    # ---- holdings ----

    def get_insider(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/holdings/insider")
        return self._unwrap_list(data)

    def get_buyback(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/holdings/buyback")
        return self._unwrap_list(data)

    def get_shorts(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/holdings/shorts")
        return self._unwrap_list(data)
