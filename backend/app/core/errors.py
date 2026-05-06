class PolyoracleError(Exception):
    """Base application error."""


class LiveTradingBlocked(PolyoracleError):
    """Raised when live trading is blocked by policy or configuration."""
