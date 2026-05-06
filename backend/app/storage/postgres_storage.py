class PostgresStorage:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def status(self) -> dict[str, bool | str]:
        return {"backend": "postgres", "enabled": self.enabled, "note": "optional advanced adapter"}
