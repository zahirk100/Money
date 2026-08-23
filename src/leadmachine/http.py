"""Beleefde HTTP-client: identificeert zichzelf, houdt afstand, leest robots.txt.

Dat is niet alleen netjes, het houdt je IP en je domein ook uit de blocklists.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass

import requests

DEFAULT_UA = "LeadMachine/1.0 (+https://example.com/bot)"


@dataclass
class Fetched:
    ok: bool
    url: str
    final_url: str
    status_code: int | None
    html: str
    elapsed_ms: int
    bytes: int
    error: str | None = None
    blocked_by_robots: bool = False


class PoliteClient:
    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        timeout: float = 12.0,
        delay: float = 1.5,
        respect_robots: bool = True,
        max_bytes: int = 2_000_000,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.delay = delay
        self.respect_robots = respect_robots
        self.max_bytes = max_bytes
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "nl,en;q=0.8",
            }
        )

    def _wait_for(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            remaining = self.delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_hit[host] = time.monotonic()

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{root}/robots.txt")
            try:
                self._wait_for(parts.netloc)
                resp = self._session.get(f"{root}/robots.txt", timeout=self.timeout)
                if resp.status_code >= 400:
                    parser = None  # geen robots.txt betekent: gewoon toegestaan
                else:
                    parser.parse(resp.text.splitlines())
            except requests.RequestException:
                parser = None
            self._robots[root] = parser
        parser = self._robots[root]
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def get(self, url: str) -> Fetched:
        if not urllib.parse.urlsplit(url).scheme:
            url = "https://" + url
        parts = urllib.parse.urlsplit(url)

        if not self._allowed(url):
            return Fetched(False, url, url, None, "", 0, 0,
                           error="robots.txt verbiedt dit", blocked_by_robots=True)

        self._wait_for(parts.netloc)
        started = time.monotonic()
        try:
            resp = self._session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(8192):
                chunks.append(chunk)
                total += len(chunk)
                if total >= self.max_bytes:
                    break
            resp.close()
            elapsed = int((time.monotonic() - started) * 1000)
            encoding = resp.encoding or "utf-8"
            html = b"".join(chunks).decode(encoding, errors="replace")
            return Fetched(
                ok=resp.status_code < 400,
                url=url,
                final_url=resp.url,
                status_code=resp.status_code,
                html=html,
                elapsed_ms=elapsed,
                bytes=total,
            )
        except requests.RequestException as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return Fetched(False, url, url, None, "", elapsed, 0, error=type(exc).__name__)
