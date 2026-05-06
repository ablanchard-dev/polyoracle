from app.providers.execution.base import ExecutionProvider


class PaperExecutionProvider(ExecutionProvider):
    def submit_order(self, payload: dict) -> dict:
        return {"status": "paper_submitted", "payload": payload}
