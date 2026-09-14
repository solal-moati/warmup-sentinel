"""Minimal lemlist client for warmup-sentinel: reads mailboxes, campaigns
and activities, plus pausing a lead (the agent's only write, capped by
lead_guard.py).

API facts verified in production (2026):
  · auth is HTTP basic with an empty username (":API_KEY");
  · rate limit 20 req / 2s → 150ms client-side throttle + retries on 429/5xx;
  · lemlist sends a FRACTIONAL Retry-After on 429s ("0.429"), which crashes
    urllib3's parser: the tolerant adapter below neutralizes it;
  · a nonexistent path returns HTTP 200 + the app's HTML shell instead of a
    404: the status proves nothing, callers validate the CONTENT (see
    warmup.is_valid).
"""

from __future__ import annotations

import logging
import math
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from lib import config
from lib._http import mount_retries

logger = logging.getLogger("lemlist")

BASE_URL = "https://api.lemlist.com/api"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LemlistAPIError(requests.exceptions.HTTPError):
    """Non-retryable lemlist HTTP error, status and body preserved."""

    def __init__(self, status_code: int, endpoint: str, body: str):
        self.status_code = status_code
        self.body = body or ""
        super().__init__(f"{status_code} on {endpoint}: {self.body[:300]}")


class _FloatTolerantRetry(Retry):
    def parse_retry_after(self, retry_after: str) -> float:
        try:
            return super().parse_retry_after(retry_after)
        except Exception:
            try:
                return min(math.ceil(float(retry_after)), float(self.retry_after_max))
            except (TypeError, ValueError):
                return 1.0


def _retry_after_seconds(resp, default: float) -> float:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return default
    try:
        return min(math.ceil(float(raw)), 60.0)
    except (TypeError, ValueError):
        return default


class LemlistClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        mount_retries(self.session)
        self.session.auth = ("", api_key)
        adapter = HTTPAdapter(max_retries=_FloatTolerantRetry(
            total=0, read=False, redirect=False, respect_retry_after_header=False))
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._last_request = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < config.LEMLIST_MIN_INTERVAL_S:
            time.sleep(config.LEMLIST_MIN_INTERVAL_S - elapsed)

    def _request(self, method: str, endpoint: str, *, params=None, json=None,
                 retries: int = 4, ok_codes=(200, 201, 202)):
        url = f"{BASE_URL}/{endpoint}"
        resp = None
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.request(method, url, params=params, json=json,
                                            timeout=60)
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise
                wait = min(2 ** attempt, 10)
                logger.warning("lemlist network error on %s (%s) — retry in %ss",
                               endpoint, e, wait)
                time.sleep(wait)
                self._last_request = time.time()
                continue
            self._last_request = time.time()
            if resp.status_code in ok_codes:
                if not resp.text:
                    return resp.status_code, {}
                try:
                    return resp.status_code, resp.json()
                except ValueError:
                    return resp.status_code, {"raw": resp.text}
            if resp.status_code == 404:
                return 404, {}
            if resp.status_code in _RETRYABLE_STATUS and attempt < retries - 1:
                wait = _retry_after_seconds(resp, default=min(2 ** attempt, 10))
                logger.warning("lemlist %s on %s — waiting %ss (attempt %d/%d)",
                               resp.status_code, endpoint, wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            raise LemlistAPIError(resp.status_code, endpoint, resp.text)
        raise LemlistAPIError(resp.status_code if resp is not None else 0, endpoint,
                              resp.text if resp is not None else "no response")

    # ── Team and mailboxes ───────────────────────────────────────────────────
    def get_team(self) -> dict:
        _, data = self._request("GET", "team")
        return data

    def get_user(self, user_id: str) -> dict:
        _, data = self._request("GET", f"users/{user_id}")
        return data if isinstance(data, dict) else {}

    def get_senders(self) -> list:
        code, data = self._request("GET", "team/senders", ok_codes=(200, 404))
        if code == 200:
            if isinstance(data, list):
                return data
            for key in ("senders", "data", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    # ── Campaigns and activities ─────────────────────────────────────────────
    def get_campaigns(self) -> list:
        campaigns, page = [], 1
        while True:
            _, data = self._request("GET", "campaigns",
                                    params={"version": "v2", "limit": 100,
                                            "page": page})
            batch = data.get("campaigns", data if isinstance(data, list) else [])
            campaigns.extend(batch)
            if len(batch) < 100:
                return campaigns
            page += 1

    def get_activities(self, campaign_id=None, activity_type=None,
                       limit: int = 100, offset: int = 0) -> list:
        params = {"version": "v2", "limit": limit, "offset": offset}
        if campaign_id:
            params["campaignId"] = campaign_id
        if activity_type:
            params["type"] = activity_type
        _, data = self._request("GET", "activities", params=params)
        return data if isinstance(data, list) else data.get("activities", [])

    # ── The agent's only write ───────────────────────────────────────────────
    def pause_lead(self, campaign_id: str, lead_ref: str) -> dict:
        """Pauses a lead, never removes it: a lead removed then re-inserted
        would restart its sequence from step 1."""
        endpoint = f"leads/pause/{lead_ref}"
        if campaign_id:
            endpoint += f"?campaignId={campaign_id}"
        _, data = self._request("POST", endpoint)
        return data
