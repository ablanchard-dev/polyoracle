class LocalWatchlistProvider:
    def __init__(self) -> None:
        self._watchlist: set[str] = set()
        self._blacklist: set[str] = set()

    def list_watchlist(self) -> list[str]:
        return sorted(self._watchlist)

    def follow(self, address: str) -> dict[str, str]:
        self._watchlist.add(address)
        self._blacklist.discard(address)
        return {"address": address, "status": "follow"}

    def blacklist(self, address: str) -> dict[str, str]:
        self._blacklist.add(address)
        self._watchlist.discard(address)
        return {"address": address, "status": "blacklist"}
