"""
http.py - Module untuk proyek
"""

import aiohttp

class HttpClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                raise_for_status=False,
                timeout=aiohttp.ClientTimeout(total=300),
                connector=aiohttp.TCPConnector(limit=100, force_close=False),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

http_client = HttpClient()
