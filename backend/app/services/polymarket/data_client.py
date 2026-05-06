from typing import Any

from app.config import get_settings
from app.services.polymarket.base import PolymarketHttpClient


class DataClient:
    """Thin wrapper over the public Polymarket Data API.

    Note: as of 2026-04, the legacy `/leaderboard` endpoint returns 404. Real wallet
    discovery now goes through `/trades` (recent global trade feed) and per-user
    `/positions` and `/activity` calls.
    """

    def __init__(self) -> None:
        self.client = PolymarketHttpClient(get_settings().data_api_base_url)

    def fetch_leaderboard(self) -> list[dict[str, Any]]:
        data = self.client.get("/leaderboard")
        return data if isinstance(data, list) else data.get("leaderboard", [])

    def fetch_wallet_positions(self, address: str) -> list[dict[str, Any]]:
        data = self.client.get("/positions", params={"user": address})
        return data if isinstance(data, list) else data.get("positions", [])

    def fetch_wallet_trades(self, address: str) -> list[dict[str, Any]]:
        data = self.client.get("/activity", params={"user": address})
        return data if isinstance(data, list) else data.get("activity", [])

    def fetch_recent_trades(self, limit: int = 500) -> list[dict[str, Any]]:
        """Recent trades across the whole market. Each entry includes proxyWallet,
        conditionId, asset, side, size, price, timestamp, outcome, slug, title."""
        data = self.client.get("/trades", params={"limit": limit})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("trades", "data", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def fetch_market_holders(self, market: str) -> list[dict[str, Any]]:
        data = self.client.get("/holders", params={"market": market})
        return data if isinstance(data, list) else data.get("holders", []) if isinstance(data, dict) else []

    def fetch_market_trades(self, condition_id: str, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        """All trades on a single market, ordered most recent first.

        The endpoint accepts a ``market`` query param keyed on the conditionId.
        Returns up to ``limit`` rows; pass an ``offset`` to paginate.
        """
        params: dict[str, Any] = {"market": condition_id, "limit": limit}
        if offset:
            params["offset"] = offset
        data = self.client.get("/trades", params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("trades", "data", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []
