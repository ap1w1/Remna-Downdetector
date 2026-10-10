from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx


INTEGRATION_ICON_URLS = {
    "regru": (
        "https://showcase-static.reg.ru/_nuxt/static/"
        "favicon.CfRoFjU7.ico?v=2026"
    ),
    "cloudflare": "https://dash.cloudflare.com/favicons/favicon.ico",
    "dpi": "https://dpichecker.st/public/web/img/favicon.ico",
}


class IconCache:
    content_types = {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/svg+xml",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "application/octet-stream",
    }

    def __init__(self, directory: Path, logger: logging.Logger) -> None:
        self.directory = directory
        self.logger = logger
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: dict[
            str,
            asyncio.Task[tuple[bytes, str] | None],
        ] = {}

    def paths(self, url: str) -> tuple[Path, Path, Path]:
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        return (
            self.directory / f"{cache_key}.img",
            self.directory / f"{cache_key}.json",
            self.directory / f"{cache_key}.failed",
        )

    @classmethod
    def content_type(cls, content: bytes, reported: str = "") -> str:
        content_type = reported.split(";", 1)[0].strip().lower()
        if content_type in cls.content_types - {"application/octet-stream"}:
            return content_type
        stripped = content.lstrip()
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith(b"\x00\x00\x01\x00"):
            return "image/x-icon"
        if stripped.startswith(b"<svg") or b"<svg" in stripped[:300]:
            return "image/svg+xml"
        raise ValueError("Unsupported icon content type")

    def read(self, url: str) -> tuple[bytes, str] | None:
        cache_file, metadata_file, _ = self.paths(url)
        if not cache_file.exists():
            return None
        content = cache_file.read_bytes()
        reported = ""
        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text("utf-8"))
                reported = str(metadata.get("content_type", ""))
            except (OSError, ValueError, AttributeError):
                reported = ""
        return content, self.content_type(content, reported)

    async def get(
        self,
        url: str,
        *,
        trusted: bool = False,
        is_allowed: Callable[[str], bool] | None = None,
    ) -> tuple[bytes, str] | None:
        if not url:
            return None
        cache_file, metadata_file, failed_file = self.paths(url)
        try:
            cached = self.read(url)
        except (OSError, ValueError):
            cached = None
        if cached:
            return cached
        if not trusted and (is_allowed is None or not is_allowed(url)):
            return None
        try:
            recently_failed = (
                failed_file.exists()
                and time.time() - failed_file.stat().st_mtime < 21600
            )
        except OSError:
            recently_failed = False
        if recently_failed:
            return None

        cache_key = cache_file.stem
        lock = self.locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                cached = self.read(url)
            except (OSError, ValueError):
                cached = None
            if cached:
                return cached
            try:
                async with httpx.AsyncClient(
                    timeout=10,
                    follow_redirects=trusted,
                ) as client:
                    response = await client.get(
                        url,
                        headers={"Accept": "image/*"},
                    )
                    response.raise_for_status()
                if not response.content or len(response.content) > 262144:
                    raise ValueError("Icon is empty or too large")
                content_type = self.content_type(
                    response.content,
                    response.headers.get("content-type", ""),
                )
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary_file = cache_file.with_suffix(".tmp")
                temporary_file.write_bytes(response.content)
                temporary_file.replace(cache_file)
                metadata_file.write_text(
                    json.dumps(
                        {
                            "content_type": content_type,
                            "cached_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    "utf-8",
                )
                failed_file.unlink(missing_ok=True)
                return response.content, content_type
            except (httpx.HTTPError, OSError, ValueError) as exc:
                try:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    failed_file.touch()
                except OSError:
                    pass
                self.logger.warning(
                    "Unable to cache icon from %s: %s",
                    urlparse(url).hostname or "unknown",
                    exc,
                )
                return None

    def schedule(
        self,
        url: str,
        *,
        trusted: bool = False,
        is_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        if not url:
            return
        cache_file, _, _ = self.paths(url)
        cache_key = cache_file.stem
        if cache_file.exists() or cache_key in self.tasks:
            return
        task = asyncio.create_task(
            self.get(url, trusted=trusted, is_allowed=is_allowed)
        )
        self.tasks[cache_key] = task

        def finished(
            completed: asyncio.Task[tuple[bytes, str] | None],
        ) -> None:
            self.tasks.pop(cache_key, None)
            try:
                completed.result()
            except Exception:
                self.logger.exception("Unexpected icon cache failure")

        task.add_done_callback(finished)
