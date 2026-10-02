"""BorsdataAdapter — single adapter implementing MarketDataProvider.

Behavior rules (plan §1.2):
- MAX_RETRIES=3 with Retry-After, batch 50, 0.5-1s sleep, original=0
- maxCount never trusted — fetch full and slice locally
- sector-KPI 400 swallowed as missing
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import requests

from alphaforge.providers.http import MAX_RETRIES, request_with_retry, sleep_between_batches

BASE_URL = "https://apiservice.borsdata.se"
BATCH_SIZE = 50


class BorsdataContractError(RuntimeError):
    """A successful provider response did not match the documented contract."""


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
        # 400 for sector-KPI mismatch → swallowed as missing (return None).
        if resp.status_code == 400:
            return None
        resp.raise_for_status()
        try:
            return self._normalize_keys(resp.json())
        except ValueError as exc:
            raise BorsdataContractError(f"{path}: 200 response was not valid JSON") from exc

    @classmethod
    def _normalize_keys(cls, value: Any) -> Any:
        """Normalize live Pascal/camel casing without changing value semantics."""
        if isinstance(value, list):
            return [cls._normalize_keys(item) for item in value]
        if isinstance(value, dict):
            return {
                (key[:1].lower() + key[1:] if key else key): cls._normalize_keys(item)
                for key, item in value.items()
            }
        return value

    @staticmethod
    def _object_rows(rows: Any, *, endpoint: str, field: str) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            raise BorsdataContractError(f"{endpoint}: {field} is not an array")
        if not all(isinstance(item, dict) for item in rows):
            raise BorsdataContractError(f"{endpoint}: {field} contains non-object rows")
        return rows

    @staticmethod
    def _report_rows(rows: Any, *, endpoint: str, field: str) -> list[dict[str, Any]]:
        rows = BorsdataAdapter._object_rows(rows, endpoint=endpoint, field=field)
        period_fields = (
            "period_End",
            "report_End_Date",
            "period_end",
            "periodEnd",
            "report_Date",
            "reportDate",
            "date",
        )
        if any(not any(row.get(key) for key in period_fields) for row in rows):
            raise BorsdataContractError(f"{endpoint}: {field} contains a row without a period end")
        return rows

    @staticmethod
    def _price_rows(rows: Any, *, endpoint: str, field: str) -> list[dict[str, Any]]:
        rows = BorsdataAdapter._object_rows(rows, endpoint=endpoint, field=field)
        date_fields = ("price_Date", "price_date", "d", "date")
        close_fields = ("close", "c", "price")
        if any(
            not any(row.get(key) is not None for key in date_fields)
            or not any(row.get(key) is not None for key in close_fields)
            for row in rows
        ):
            raise BorsdataContractError(f"{endpoint}: {field} contains an incomplete price row")
        return rows

    def _unwrap_list(self, data: Any, *, endpoint: str) -> list[dict[str, Any]]:
        if data is None:
            return []
        if isinstance(data, list):
            return self._object_rows(data, endpoint=endpoint, field="response")
        if isinstance(data, dict):
            # Börsdata has used several envelope names for the same resource.
            # A 200 object with none of these is a contract failure, not empty
            # data: silently treating it as empty corrupts the database.
            keys = (
                "instruments",
                "reports",
                "markets",
                "branches",
                "sectors",
                "countries",
                "kpis",
                "stockPricesList",
                "stockPrices",
                "dividends",
                "dividendList",
                "insider",
                "insiderList",
                "buyback",
                "buybackList",
                "shorts",
                "shortsList",
                "calendarList",
                "list",
                "stockSplits",
                "stockSplitList",
                "reportCalendar",
                "translationMetadata",
                "translationMetadatas",
                "kpiMetadata",
                "kpiMetadatas",
                "kpiHistoryMetadatas",
                "reportMetadata",
                "reportMetadatas",
                "values",
            )
            for key in keys:
                if key in data:
                    return self._object_rows(data[key], endpoint=endpoint, field=key)
            if "instrument" in data:
                return [data]
        raise BorsdataContractError(f"{endpoint}: unrecognized 200 response shape")

    # ---- reference dictionaries (should-adds) ----

    def get_instruments(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments")
        return self._unwrap_list(data, endpoint="/v1/instruments")

    def get_markets(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/markets")
        return self._unwrap_list(data, endpoint="/v1/markets")

    def get_branches(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/branches")
        return self._unwrap_list(data, endpoint="/v1/branches")

    def get_sectors(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/sectors")
        return self._unwrap_list(data, endpoint="/v1/sectors")

    def get_countries(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/countries")
        return self._unwrap_list(data, endpoint="/v1/countries")

    def get_translation_metadata(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/translationmetadata")
        return self._unwrap_list(data, endpoint="/v1/translationmetadata")

    def get_kpi_metadata(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments/kpis/metadata")
        return self._unwrap_list(data, endpoint="/v1/instruments/kpis/metadata")

    def get_report_metadata(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/instruments/reports/metadata")
        return self._unwrap_list(data, endpoint="/v1/instruments/reports/metadata")

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
        raise BorsdataContractError(f"{path}: unrecognized 200 response shape")

    def get_kpi_history(
        self, ins_id: int, kpi_id: int, report_type: str, price_type: str
    ) -> list[dict[str, Any]]:
        # GET /v1/instruments/{id}/kpis/{kpiId}/{reportType}/{priceType}/history
        path = f"/v1/instruments/{ins_id}/kpis/{kpi_id}/{report_type}/{price_type}/history"
        data = self._get_json(path)
        if data is None:
            return []  # 400 → missing
        if isinstance(data, dict):
            for key in ("values", "kpiHistory", "kpiHistoryMetadatas"):
                if key in data:
                    return self._object_rows(data[key], endpoint=path, field=key)
        if isinstance(data, list):
            return self._object_rows(data, endpoint=path, field="response")
        raise BorsdataContractError(f"{path}: unrecognized 200 response shape")

    def get_kpi_summary(self, ins_id: int, report_type: str) -> dict[str, Any] | None:
        # GET /v1/instruments/{insid}/kpis/{reporttype}/summary
        path = f"/v1/instruments/{ins_id}/kpis/{report_type}/summary"
        data = self._get_json(path)
        if data is None or isinstance(data, list) and len(data) == 0:
            return None
        if isinstance(data, dict) and data:
            return data
        raise BorsdataContractError(f"{path}: unrecognized 200 response shape")

    @staticmethod
    def _flatten_report_envelope(data: Any, *, endpoint: str) -> list[dict[str, Any]]:
        """Flatten live ``reportList`` period arrays into canonical report rows."""
        if isinstance(data, list):
            return BorsdataAdapter._report_rows(data, endpoint=endpoint, field="response")
        if not isinstance(data, dict) or "reportList" not in data:
            # Older responses use a direct reports array.
            if isinstance(data, dict) and "reports" in data:
                return BorsdataAdapter._report_rows(
                    data["reports"], endpoint=endpoint, field="reports"
                )
            raise BorsdataContractError(f"{endpoint}: unrecognized 200 response shape")
        flattened: list[dict[str, Any]] = []
        report_lists = data["reportList"]
        if isinstance(report_lists, dict):
            report_lists = [report_lists]
        report_lists = BorsdataAdapter._object_rows(
            report_lists, endpoint=endpoint, field="reportList"
        )
        for instrument in report_lists:
            for key, period_type in (
                ("reportsYear", "year"),
                ("reportsQuarter", "quarter"),
                ("reportsR12", "r12"),
            ):
                rows = instrument.get(key, [])
                if isinstance(rows, dict):
                    rows = [rows]
                rows = BorsdataAdapter._report_rows(rows, endpoint=endpoint, field=key)
                for row in rows:
                    item = dict(row)
                    item.setdefault("period_type", period_type)
                    item.setdefault(
                        "insId",
                        instrument.get("insId")
                        or instrument.get("instrumentId")
                        or instrument.get("instrument"),
                    )
                    flattened.append(item)
        return flattened

    # ---- reports (batch ≤50, original=0) ----

    def get_reports(
        self,
        ins_ids: list[int],
        *,
        original: int = 0,
        target_currencies: Mapping[int, str | None] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch reports and bind their requested mode/target acquisition metadata."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(ins_ids), BATCH_SIZE):
            chunk = ins_ids[i : i + BATCH_SIZE]
            params: dict[str, Any] = {
                "instList": ",".join(str(x) for x in chunk),
                "original": original,
            }
            data = self._get_json("/v1/instruments/reports", params=params)
            rows = self._flatten_report_envelope(data, endpoint="/v1/instruments/reports")
            mode = "converted" if original == 0 else "original" if original == 1 else "unknown"
            for row in rows:
                ins_id = row.get("insId") or row.get("instrumentId") or row.get("instrument")
                try:
                    target = target_currencies.get(int(ins_id)) if target_currencies else None
                except (TypeError, ValueError):
                    target = None
                original_currency = row.get("currency")
                values_currency = target if mode == "converted" else original_currency
                row["conversion_mode"] = mode
                row["conversion_target_currency"] = target
                row["values_currency"] = values_currency
            out.extend(rows)
            if i + BATCH_SIZE < len(ins_ids):
                sleep_between_batches()
        return out

    def get_report_calendar(self, ins_ids: list[int] | None = None) -> list[dict[str, Any]]:
        params = {"instList": ",".join(str(item) for item in ins_ids)} if ins_ids else None
        data = self._get_json("/v1/instruments/report/calendar", params=params)
        rows = self._unwrap_list(data, endpoint="/v1/instruments/report/calendar")
        # The live endpoint may return one object per instrument with a nested
        # calendar array, while older responses return flat rows.
        flattened: list[dict[str, Any]] = []
        for row in rows:
            nested_key = next(
                (key for key in ("calendar", "reportCalendar", "values") if key in row),
                None,
            )
            instrument = row.get("insId") or row.get("instrumentId") or row.get("instrument")
            if nested_key is not None:
                nested = row[nested_key]
                if not isinstance(nested, list):
                    raise BorsdataContractError(
                        f"/v1/instruments/report/calendar: {nested_key} is not an array"
                    )
                for event in nested:
                    if not isinstance(event, dict):
                        raise BorsdataContractError(
                            "/v1/instruments/report/calendar: "
                            f"{nested_key} contains non-object rows"
                        )
                    if not any(event.get(key) for key in ("releaseDate", "date")):
                        raise BorsdataContractError(
                            "/v1/instruments/report/calendar: nested row has no release date"
                        )
                    item = dict(event)
                    if instrument is not None:
                        item.setdefault("insId", instrument)
                    flattened.append(item)
            else:
                if not any(row.get(key) for key in ("releaseDate", "date")):
                    raise BorsdataContractError(
                        "/v1/instruments/report/calendar: response row has no release date"
                    )
                flattened.append(row)
        return flattened

    def get_stock_splits(self, *, from_date: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        data = self._get_json("/v1/instruments/StockSplits", params=params or None)
        return self._unwrap_list(data, endpoint="/v1/instruments/StockSplits")

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
        endpoint = f"/v1/instruments/{ins_id}/stockprices"
        if isinstance(data, dict):
            if "stockPricesList" in data:
                rows = self._price_rows(
                    data["stockPricesList"], endpoint=endpoint, field="stockPricesList"
                )
            elif "stockPrices" in data:
                rows = self._price_rows(data["stockPrices"], endpoint=endpoint, field="stockPrices")
            else:
                rows = self._unwrap_list(data, endpoint=endpoint)
        elif isinstance(data, list):
            rows = self._price_rows(data, endpoint=endpoint, field="response")
        else:
            rows = self._unwrap_list(data, endpoint=endpoint)
        # Sort by date (n ascending = oldest first as per API), then slice tail if requested
        # Börsdata returns 10y default sorted asc; we keep order and slice locally.
        if max_count is not None and len(rows) > max_count:
            rows = rows[-max_count:]
        return rows

    # ---- dividends ----

    @staticmethod
    def _is_zero_dividend(row: dict[str, Any]) -> bool:
        amount = row.get("amountPaid") if "amountPaid" in row else row.get("amount")
        try:
            return amount is not None and float(amount) == 0.0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _dividend_ex_date(row: dict[str, Any]) -> Any:
        return next(
            (
                row.get(key)
                for key in ("exDate", "ex_date", "excludingDate", "date")
                if row.get(key)
            ),
            None,
        )

    @classmethod
    def _canonicalize_dividend(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Map Börsdata's live ``excludingDate`` field to the domain ex-date."""
        item = dict(row)
        if "exDate" not in item:
            ex_date = cls._dividend_ex_date(item)
            if ex_date is not None:
                item["exDate"] = ex_date
        return item

    def get_dividends(self, ins_ids: list[int] | None = None) -> list[dict[str, Any]]:
        """Calendar observations only; the endpoint provides no window completeness assurance."""
        params = {"instList": ",".join(str(item) for item in ins_ids)} if ins_ids else None
        data = self._get_json("/v1/instruments/dividend/calendar", params=params)
        rows = self._unwrap_list(data, endpoint="/v1/instruments/dividend/calendar")
        flattened: list[dict[str, Any]] = []
        for row in rows:
            nested_key = next(
                (key for key in ("dividends", "dividendList", "values") if key in row),
                None,
            )
            instrument = row.get("insId") or row.get("instrumentId") or row.get("instrument")
            if nested_key is not None:
                nested = row[nested_key]
                if not isinstance(nested, list):
                    raise BorsdataContractError(
                        f"/v1/instruments/dividend/calendar: {nested_key} is not an array"
                    )
                for dividend in nested:
                    if not isinstance(dividend, dict):
                        raise BorsdataContractError(
                            "/v1/instruments/dividend/calendar: "
                            f"{nested_key} contains non-object rows"
                        )
                    # Undated zero markers have no window identity; dated zeros
                    # are real observations, not a completeness assertion.
                    if (
                        self._is_zero_dividend(dividend)
                        and self._dividend_ex_date(dividend) is None
                    ):
                        continue
                    if self._dividend_ex_date(dividend) is None:
                        raise BorsdataContractError(
                            "/v1/instruments/dividend/calendar: nested row has no ex-date"
                        )
                    item = self._canonicalize_dividend(dividend)
                    if instrument is not None:
                        item.setdefault("insId", instrument)
                    flattened.append(item)
            else:
                if self._is_zero_dividend(row) and self._dividend_ex_date(row) is None:
                    continue
                if self._dividend_ex_date(row) is None:
                    raise BorsdataContractError(
                        "/v1/instruments/dividend/calendar: response row has no ex-date"
                    )
                flattened.append(self._canonicalize_dividend(row))
        return flattened

    # ---- holdings ----

    def get_insider(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/holdings/insider")
        return self._unwrap_list(data, endpoint="/v1/holdings/insider")

    def get_buyback(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/holdings/buyback")
        return self._unwrap_list(data, endpoint="/v1/holdings/buyback")

    def get_shorts(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/holdings/shorts")
        return self._unwrap_list(data, endpoint="/v1/holdings/shorts")
