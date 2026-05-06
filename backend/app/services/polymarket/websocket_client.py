class WebSocketManager:
    def __init__(self) -> None:
        self.connected = False

    async def connect_market_stream(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
