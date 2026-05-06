from typing import Any

from app.config import get_settings
from app.services.polymarket.base import PolymarketHttpClient


class GammaClient:
    def __init__(self, timeout: float | None = None) -> None:
        if timeout is not None:
            self.client = PolymarketHttpClient(get_settings().gamma_api_base_url, timeout=timeout)
        else:
            self.client = PolymarketHttpClient(get_settings().gamma_api_base_url)

    def fetch_active_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        data = self.client.get("/markets", params={"active": "true", "closed": "false", "limit": limit})
        return data if isinstance(data, list) else data.get("markets", [])

    def fetch_market_details(self, market_id: str) -> dict[str, Any]:
        data = self.client.get(f"/markets/{market_id}")
        return data if isinstance(data, dict) else {}

    def fetch_closed_markets(
        self,
        limit: int = 200,
        offset: int = 0,
        order: str = "endDate",
        ascending: bool = False,
        end_date_max: str | None = None,
    ) -> list[dict[str, Any]]:
        """Markets whose ``closed`` flag is true. Returns most-recently-ended first by default."""
        params: dict[str, Any] = {
            "closed": "true",
            "limit": limit,
            "order": order,
            "ascending": "true" if ascending else "false",
        }
        if offset:
            params["offset"] = offset
        if end_date_max is not None:
            params["end_date_max"] = end_date_max
        data = self.client.get("/markets", params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("markets", "data", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def fetch_market_by_condition(self, condition_id: str) -> dict[str, Any]:
        """Look up a market by its on-chain conditionId.

        v0.7.8 BUGFIX — gamma's singular ``condition_id`` filter is BROKEN
        (silently ignored, returns the first market in the index). The
        correct param name is ``condition_ids`` (plural). We also fall
        back to ``closed=true`` for resolved markets, which is the
        common case for backfills (markets we polled but that have
        since settled).

        Returns ``{}`` when no market matches — never a misleading fallback.
        """
        def _normalize(data):
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                results = data.get("markets") or data.get("data")
                if isinstance(results, list) and results:
                    return results[0]
            return {}

        # Try active first (open markets)
        data = self.client.get(
            "/markets", params={"condition_ids": condition_id, "limit": 1}
        )
        result = _normalize(data)
        # Sanity check: confirm the returned market actually has this condition_id
        if result and result.get("conditionId") == condition_id:
            return result

        # Fall back to closed/resolved markets
        data_closed = self.client.get(
            "/markets",
            params={"condition_ids": condition_id, "closed": "true", "limit": 1},
        )
        result_closed = _normalize(data_closed)
        if result_closed and result_closed.get("conditionId") == condition_id:
            return result_closed

        return {}
