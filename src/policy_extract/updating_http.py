"""Minimal stdlib HTTP JSON client for the update flow."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from policy_extract.updating_errors import UpdateError

_USER_AGENT = "policy-extract-updater"


def http_get_json(url: str, *, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"update API unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError(f"update API response is not JSON: {exc}") from exc
