"""warmup-sentinel configuration. Everything else is set through environment
variables or the .env file (see .env.example)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# lemlist rate limit is 20 requests / 2s → 150ms client-side throttle.
LEMLIST_MIN_INTERVAL_S = 0.15
