from abc import ABC, abstractmethod


class ExecutionProvider(ABC):
    @abstractmethod
    def submit_order(self, payload: dict) -> dict:
        raise NotImplementedError
