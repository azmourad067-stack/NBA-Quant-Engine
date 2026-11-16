from __future__ import annotations

import time
from typing import Any

import requests


ENDPOINT_COSTS = {
    "list_teams": 1,
    "list_games": 2,
    "get_game": 5,
    "get_odds": 5,  # one game; DunkAPI documents 10 credits for a date batch
}


class DunkAPIError(RuntimeError):
    pass


class CreditBudgetExceeded(DunkAPIError):
    pass


class CreditBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def reserve(self, endpoint: str, estimated_cost: int | None = None) -> None:
        cost = estimated_cost if estimated_cost is not None else ENDPOINT_COSTS[endpoint]
        if self.used + cost > self.limit:
            raise CreditBudgetExceeded(
                f"Credit budget would be exceeded: used={self.used}, "
                f"estimated_next={cost}, limit={self.limit}."
            )

    def record(self, actual_cost: int) -> None:
        if actual_cost > 0:
            self.used += actual_cost


class DunkAPIClient:
    """Thin REST client for DunkAPI's documented POST /v1/{tool} endpoints."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://mcp.dunkapi.com",
        timeout: int = 60,
        interval_seconds: float = 1.05,
        budget: CreditBudget | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.interval_seconds = interval_seconds
        self.budget = budget
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NBA-Quant-Engine/0.1",
        })
        self._last_request_at = 0.0

    def close(self) -> None:
        self.session.close()

    def post(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        if endpoint not in ENDPOINT_COSTS:
            raise ValueError(f"Unsupported DunkAPI endpoint: {endpoint}")

        estimated_cost = ENDPOINT_COSTS[endpoint]
        if endpoint == "get_odds" and "date" in payload:
            estimated_cost = 10
        if self.budget:
            self.budget.reserve(endpoint, estimated_cost)

        wait_for = self.interval_seconds - (time.monotonic() - self._last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)

        url = f"{self.base_url}/v1/{endpoint}"
        response = None
        for attempt in range(4):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                self._last_request_at = time.monotonic()
                if attempt == 3:
                    raise DunkAPIError(f"Network error calling {endpoint}: {exc}") from exc
                time.sleep(min(2 ** attempt, 12))
                continue
            finally:
                self._last_request_at = time.monotonic()

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else min(2 ** attempt, 12)
                    except ValueError:
                        delay = min(2 ** attempt, 12)
                    time.sleep(max(0.5, delay))
                    continue
            break

        if response is None:
            raise DunkAPIError(f"No response received from DunkAPI endpoint {endpoint}.")

        headers = {k.lower(): v for k, v in response.headers.items()}
        if response.status_code >= 400:
            # Failed server calls are documented as refunded; do not add to local budget.
            try:
                body = _safe_json(response)
                message = body.get("message") or body.get("error") or response.text[:500]
            except DunkAPIError:
                message = response.text[:500]
            raise DunkAPIError(f"DunkAPI {endpoint} returned HTTP {response.status_code}: {message}")

        body = _safe_json(response)
        if body.get("error"):
            raise DunkAPIError(f"DunkAPI {endpoint}: {body.get('message') or body['error']}")

        data = body.get("data", body)
        if not isinstance(data, dict):
            raise DunkAPIError(f"Unexpected response shape from {endpoint}: expected a JSON object.")

        if self.budget:
            actual = _int_or_none(headers.get("x-credits-used"))
            if actual is None:
                actual = estimated_cost
            self.budget.record(actual)

        remaining = _int_or_none(headers.get("x-credits-remaining"))
        if remaining is not None and remaining < 100:
            # DunkAPI documents a reduced 10 requests/minute limit below 100 credits.
            self.interval_seconds = max(self.interval_seconds, 6.1)

        return data, headers


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise DunkAPIError(f"DunkAPI returned non-JSON content (HTTP {response.status_code}).") from exc
    if not isinstance(value, dict):
        raise DunkAPIError("DunkAPI returned an unexpected non-object JSON response.")
    return value


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
