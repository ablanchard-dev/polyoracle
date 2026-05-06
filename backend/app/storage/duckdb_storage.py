class DuckDBAnalyticsStorage:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def status(self) -> dict[str, bool | str]:
        return {"backend": "duckdb", "enabled": self.enabled, "note": "optional analytics adapter"}
