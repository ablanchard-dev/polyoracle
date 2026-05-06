from app.providers.execution.base import ExecutionProvider


class LiveExecutionProvider(ExecutionProvider):
    def submit_order(self, payload: dict) -> dict:
        raise PermissionError("Live execution provider is disabled by default")
