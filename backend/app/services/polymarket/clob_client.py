from typing import Any

from app.config import get_settings
from app.services.polymarket.base import PolymarketHttpClient


class ClobPublicClient:
    def __init__(self) -> None:
        self.client = PolymarketHttpClient(get_settings().clob_api_base_url)

    def fetch_orderbook(self, token_id: str) -> dict[str, Any]:
        data = self.client.get("/book", params={"token_id": token_id})
        return data if isinstance(data, dict) else {}

    def fetch_midpoint(self, token_id: str) -> dict[str, Any]:
        data = self.client.get("/midpoint", params={"token_id": token_id})
        return data if isinstance(data, dict) else {}

    def fetch_price(self, token_id: str, side: str = "BUY") -> dict[str, Any]:
        data = self.client.get("/price", params={"token_id": token_id, "side": side})
        return data if isinstance(data, dict) else {}


class ClobPrivateClient:
    def __init__(self) -> None:
        self.client = PolymarketHttpClient(get_settings().clob_api_base_url)

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise PermissionError("Private CLOB trading is disabled in v0.1 without explicit live setup")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise PermissionError("Private CLOB trading is disabled in v0.1 without explicit live setup")
