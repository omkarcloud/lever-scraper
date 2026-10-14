"""Configuration for the Lever Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT                       port the API listens on (default 8000)
    LEVER_PROXY                proxy URL for every request, e.g. http://user:pass@host:port
                               (default: none — direct). Lever's public job
                               APIs have no bot protection and did not block
                               1,500+ board reads from one IP, so you very
                               likely don't need this.
    LEVER_INDEX_WARM_AT_START  read every company in lever/directory.json into
                               the cross-company search index at start-up, in
                               the background (default 1; ~6 minutes for
                               1,500+ companies). Set 0 to skip: /jobs/search
                               then covers only the companies you name.

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors and blocks (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

LEVER_PROXY = os.environ.get("LEVER_PROXY") or None
LEVER_BLOCK_COOLDOWN = 900

# Cross-company search index (lever/index.py).
LEVER_INDEX_TTL = 3600                   # seconds a company's jobs stay fresh
LEVER_INDEX_WORKERS = 4                  # parallel company reads while refreshing
LEVER_INDEX_WARM_AT_START = os.environ.get("LEVER_INDEX_WARM_AT_START", "1") not in ("0", "false", "no")
LEVER_INDEX_IDLE_AFTER = 6 * 3600        # stop refreshing after this long without a search
LEVER_INDEX_REFRESH_POLL = 60            # seconds between refresher passes


def lever_proxy():
    """Proxy for every request (None = direct)."""
    return os.environ.get("LEVER_PROXY") or None


def lever_fallback_proxy():
    """Proxy to switch to after a block (None = keep the current route)."""
    return None
