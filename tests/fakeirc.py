"""In-process IRC server (plain ws://) for TwitchIrc reconnect/backoff/send tests."""

from __future__ import annotations

import asyncio

from websockets.asyncio.server import serve


class FakeIrcServer:
    def __init__(self):
        self.received: list[str] = []
        self.connections: list = []
        self.connected = asyncio.Event()
        self._server = None
        self.port: int = 0
        self.url: str = ""

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._server = None

    async def _handle(self, ws) -> None:
        self.connections.append(ws)
        self.connected.set()
        try:
            async for raw in ws:
                self.received.append(str(raw))
        except Exception:
            pass
        finally:
            if ws in self.connections:
                self.connections.remove(ws)

    async def wait_for(self, predicate, timeout: float = 5.0) -> str:
        """First received line matching `predicate`, else TimeoutError."""
        async with asyncio.timeout(timeout):
            while True:
                for line in list(self.received):
                    if predicate(line):
                        return line
                await asyncio.sleep(0.01)

    def count(self, predicate) -> int:
        return sum(1 for line in self.received if predicate(line))

    async def feed(self, line: str) -> None:
        """Send one raw line to every connected client (closed sockets ignored)."""
        await asyncio.gather(
            *(ws.send(line) for ws in list(self.connections)), return_exceptions=True
        )

    async def close_all(self) -> None:
        for ws in list(self.connections):
            await ws.close()
        self.connections.clear()
