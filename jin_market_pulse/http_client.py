from __future__ import annotations

import ipaddress
import logging
import random
import socket
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .config import Settings


RETRY_STATUS = {429, 500, 502, 503, 504}


class ProviderRequestError(RuntimeError):
    def __init__(self, provider: str, message: str):
        super().__init__(f"{provider}: {message}")
        self.provider = provider


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = getattr(response, "headers", {}).get("Retry-After", "").strip()
        try:
            return min(max(float(retry_after), 0.0), 8.0)
        except ValueError:
            pass
    return min(0.4 * (2**attempt) + random.uniform(0.0, 0.25), 4.0)


def request(
    method: str,
    url: str,
    settings: Settings,
    *,
    provider: str,
    attempts: int = 3,
    timeout: int | None = None,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Response:
    client = session or requests.Session()
    last_error: Exception | None = None
    for attempt in range(max(attempts, 1)):
        response: requests.Response | None = None
        try:
            requester = getattr(client, "request", None)
            if requester is None:
                requester = getattr(client, method.lower())
                response = requester(
                    url,
                    timeout=timeout or settings.request_timeout_seconds,
                    **kwargs,
                )
            else:
                response = requester(
                    method,
                    url,
                    timeout=timeout or settings.request_timeout_seconds,
                    **kwargs,
                )
            status_code = int(getattr(response, "status_code", 200))
            if status_code not in RETRY_STATUS:
                response.raise_for_status()
                return response
            last_error = ProviderRequestError(
                provider,
                f"HTTP {status_code}",
            )
        except requests.RequestException as exc:
            last_error = exc
        if attempt + 1 < max(attempts, 1):
            time.sleep(_retry_delay(response, attempt))
    logging.warning(
        "Provider request failed: provider=%s endpoint=%s",
        provider,
        urlparse(url).path,
    )
    if isinstance(last_error, ProviderRequestError):
        raise last_error
    raise ProviderRequestError(provider, "request failed") from last_error


def is_safe_public_https_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        }
        if not addresses:
            return False
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                return False
        return True
    except (OSError, ValueError):
        return False
