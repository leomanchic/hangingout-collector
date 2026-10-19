"""Small bounded HTTP client helpers for catalogue providers."""
import asyncio
from html.parser import HTMLParser
import json

import httpx


class SyncError(Exception):
    """Only safe operator-facing codes, never response bodies or credentials."""


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean(value, limit):
    if not isinstance(value, str):
        return ""
    parser = PlainText()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())[:limit]


async def request_json(client, url, params, *, headers=None):
    for attempt in range(3):
        try:
            # Do not forward credentials through redirects, even with a supplied client.
            async with client.stream("GET", url, params=params, headers=headers, follow_redirects=False) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 2:
                        raise SyncError(f"http_{response.status_code}")
                    retry = response.headers.get("Retry-After", str(2 ** attempt))
                    if not retry.isdigit() or int(retry) > 30:
                        raise SyncError(f"http_{response.status_code}_retry_later")
                    await asyncio.sleep(int(retry))
                    continue
                if response.status_code != 200:
                    raise SyncError(f"http_{response.status_code}")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8_000_000:
                        raise SyncError("page_too_large")
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                raise SyncError("invalid_json") from None
            if not isinstance(payload, dict):
                raise SyncError("invalid_page_schema")
            return payload
        except httpx.TransportError:
            if attempt == 2:
                raise SyncError("network_error") from None
            await asyncio.sleep(2 ** attempt)
