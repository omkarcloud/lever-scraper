"""Lever transport: plain curl_cffi with browser-impersonated TLS. No
browser, no login, no token, no cookies (validated 2026-10-14 from direct
Indian egress: every surface below answers 200 with no anti-bot in front).

Lever hosts a careers board per customer at jobs.lever.co/<slug> (or
jobs.eu.lever.co/<slug> for accounts on the EU shard — a slug lives on
exactly one of the two). Everything the board shows comes from Lever's own
public Postings API, which needs no key:

  * API    https://api.lever.co/v0/postings/<slug>?mode=json
           https://api.eu.lever.co/v0/postings/<slug>?mode=json
           The whole board as a JSON array (no paging needed: `skip`/`limit`
           exist but a board is at most a few hundred rows), filterable
           upstream by team / department / location / commitment (exact,
           case-sensitive, OR within a param) or grouped (`group=`).
           /<slug>/<posting id>?mode=json is one posting. A slug that is
           not on that shard answers 404 {"ok":false,"error":"Document not
           found"}; an empty board answers 200 [].

  * PAGES  https://jobs.lever.co/<slug>              the board (company name,
           logo, website, filter bar — nothing the API carries)
           https://jobs.lever.co/<slug>/<id>         the posting (schema.org
           JobPosting JSON-LD with the hiring organization)
           https://jobs.lever.co/<slug>/<id>/apply   the application form
           (standard fields, custom question cards and demographic surveys
           as JSON in hidden baseTemplate inputs, hCaptcha)

Region: the first call for a slug probes the US host and, on 404, the EU
host; the answer is memoised per slug (and seeded from the directory).

Blocks: none seen. If the pod's egress ever starts answering 403/429 (or
HTML where JSON is expected), a worker moves its session to a sticky
residential exit (config.lever_fallback_proxy()) for
config.LEVER_BLOCK_COOLDOWN seconds — the woolworths/vinted shape.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  LeverUpstreamError  transport failure / 5xx              — retryable
  LeverBlocked        403 / 429 / HTML where JSON expected — retryable on a new exit
  LeverBadRequest     upstream 400                         — never retried
  LeverNotFound       board / posting does not exist       — never retried
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from lever import refs

IMPERSONATE = "chrome"
TIMEOUT = 40
PAGE_TIMEOUT = 40
FANOUT_WORKERS = 8
REGIONS = ("us", "eu")

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
}
PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "upgrade-insecure-requests": "1",
}


class LeverUpstreamError(UpstreamError):
    """Transport failure or 5xx — retryable."""


class LeverBlocked(LeverUpstreamError, Blocked):
    """403 / 429 or an HTML body where JSON was expected — retryable."""


class LeverBadRequest(BadRequest):
    """Upstream 400 — the params were rejected. Never retried."""


class LeverNotFound(NotFound):
    """Board / posting does not exist — never retried."""


# ---- sessions ------------------------------------------------------------------
# One curl session per worker thread. Direct egress by default; after a block
# the thread's sessions go through a sticky residential exit for a cooldown.
_local = threading.local()
_fallback_until = 0.0
_fallback_lock = threading.Lock()


def _proxy_for_new_session():
    forced = config.lever_proxy()
    if forced:
        return forced
    if time.time() < _fallback_until:
        return config.lever_fallback_proxy()
    return None


def _session():
    sess = getattr(_local, "session", None)
    if sess is None:
        from curl_cffi import requests as curl_requests
        sess = curl_requests.Session(impersonate=IMPERSONATE)
        proxy = _proxy_for_new_session()
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        _local.session = sess
    return sess


def _drop_session():
    sess = getattr(_local, "session", None)
    _local.session = None
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def _note_block():
    global _fallback_until
    if config.lever_fallback_proxy() is not None:
        with _fallback_lock:
            _fallback_until = time.time() + config.LEVER_BLOCK_COOLDOWN


def dump_debug(name, body):
    dbg = os.environ.get("LEVER_DEBUG_DIR", "")
    if dbg and body:
        try:
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, name + ".txt"), "w", encoding="utf-8") as f:
                f.write(body)
        except OSError:
            pass


# ---- region memo ------------------------------------------------------------------
_regions = {}
_regions_lock = threading.Lock()


def known_region(slug):
    with _regions_lock:
        return _regions.get(slug)


def remember_region(slug, region):
    if slug and region in REGIONS:
        with _regions_lock:
            _regions[slug] = region


# ---- requests --------------------------------------------------------------------

def _classify(resp, label):
    status = resp.status_code
    if status == 404:
        raise LeverNotFound(f"{label} not found")
    if status in (403, 429):
        dump_debug("blocked", resp.text)
        _note_block()
        raise LeverBlocked(f"HTTP {status} on {label}")
    if status == 400:
        raise LeverBadRequest(f"upstream rejected {label}")
    if status >= 500 or status != 200:
        raise LeverUpstreamError(f"HTTP {status} on {label}")


def _json_of(resp, label):
    body = resp.text or ""
    if not body.lstrip().startswith(("{", "[")):
        dump_debug("nonjson", body)
        raise LeverBlocked(f"{label} returned HTML, not JSON")
    try:
        return resp.json()
    except Exception:
        dump_debug("badjson", body)
        raise LeverUpstreamError(f"could not parse JSON from {label}")


def _retrying(fn):
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return fn()
        except LeverUpstreamError as e:      # includes LeverBlocked
            last = e
            _drop_session()
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


def get_json(url, params=None, *, label=None):
    label = label or url
    query = {k: v for k, v in (params or {}).items() if v is not None}

    def once():
        try:
            resp = _session().get(url, params=query or None, headers=API_HEADERS, timeout=TIMEOUT,
                                  allow_redirects=True)
        except Exception as e:
            raise LeverUpstreamError(f"request failed: {type(e).__name__}: {e}")
        _classify(resp, label)
        return _json_of(resp, label)

    return _retrying(once)


def get_page(url, *, label=None, headers=None, timeout=PAGE_TIMEOUT):
    """GET one HTML page -> its text."""
    label = label or url

    def once():
        try:
            resp = _session().get(url, headers=headers or PAGE_HEADERS, timeout=timeout, allow_redirects=True)
        except Exception as e:
            raise LeverUpstreamError(f"request failed: {type(e).__name__}: {e}")
        _classify(resp, label)
        return resp.text or ""

    return _retrying(once)


def _regions_to_try(slug, region=None):
    known = region or known_region(slug)
    return (known,) if known else REGIONS


def _api_call(slug, region, path, params, label):
    """One postings-API call on one shard; LeverNotFound on a 404."""
    doc = get_json(f"{refs.API_HOST[region]}/{slug}{path}", dict(params or {}, mode="json"), label=label)
    if isinstance(doc, dict) and doc.get("ok") is False:
        raise LeverNotFound(f"{label} not found")
    return doc


def _on_some_region(slug, region, call, what):
    """Run call(region) on the known shard, else US then EU; a 404 on every
    shard is the board not existing."""
    for candidate in _regions_to_try(slug, region):
        try:
            result = call(candidate)
        except LeverNotFound:
            continue
        remember_region(slug, candidate)
        return result, candidate
    raise LeverNotFound(f"{what}: '{slug}' is not a Lever job board (no board on jobs.lever.co or jobs.eu.lever.co)")


def postings(slug, region=None, **params):
    """Every posting of a board -> (rows, region). Upstream filters (team,
    department, location, commitment, group, skip, limit) pass through."""
    def call(candidate):
        doc = _api_call(slug, candidate, "", params, f"postings/{slug}")
        if not isinstance(doc, list):
            raise LeverUpstreamError(f"postings/{slug}: unexpected payload shape")
        return doc
    return _on_some_region(slug, region, call, "company")


def posting(slug, job_id, region=None):
    """One posting -> (raw, region). 404 means either the board or the
    posting is gone; the caller words it."""
    def call(candidate):
        doc = _api_call(slug, candidate, f"/{job_id}", {}, f"postings/{slug}/{job_id}")
        if not isinstance(doc, dict) or not doc.get("id"):
            raise LeverUpstreamError(f"postings/{slug}/{job_id}: unexpected payload shape")
        return doc
    try:
        return _on_some_region(slug, region, call, "job")
    except LeverNotFound:
        raise LeverNotFound(f"job '{job_id}' not found on the '{slug}' board (closed, or the id / company is wrong)")


def probe(slug):
    """Does a board exist? -> (region, open posting count) or None. Never
    raises for a missing board; transport failures propagate."""
    try:
        rows, region = postings(slug)
    except LeverNotFound:
        return None
    return region, len(rows)


def board_html(slug, region=None):
    def call(candidate):
        return get_page(f"{refs.board_link(slug, candidate)}", label=f"board/{slug}")
    return _on_some_region(slug, region, call, "company")


def posting_html(slug, job_id, region=None):
    def call(candidate):
        return get_page(refs.job_link(slug, job_id, candidate), label=f"posting/{slug}/{job_id}")
    return _on_some_region(slug, region, call, "job")


def apply_html(slug, job_id, region=None):
    def call(candidate):
        return get_page(refs.apply_link(slug, job_id, candidate), label=f"apply/{slug}/{job_id}")
    try:
        return _on_some_region(slug, region, call, "job")
    except LeverNotFound:
        raise LeverNotFound(f"job '{job_id}' not found on the '{slug}' board (closed, or the id / company is wrong)")


def site_html(url, timeout=20):
    """A third-party page (a company's own careers page, for resolve). Any
    failure is None — the lookup just moves on to slug guesses."""
    try:
        resp = _session().get(url, headers=PAGE_HEADERS, timeout=timeout, allow_redirects=True)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" not in ctype and "text" not in ctype:
        return None
    return resp.text or ""


# ---- fan-out ------------------------------------------------------------------------

_fanout = ThreadPoolExecutor(max_workers=FANOUT_WORKERS * 3, thread_name_prefix="lever-fanout")


def run_parallel(fns):
    if not fns:
        return []
    if len(fns) == 1:
        return [fns[0]()]
    futures = [_fanout.submit(fn) for fn in fns[1:]]
    first = fns[0]()
    return [first] + [f.result() for f in futures]


def soft(fn, default=None):
    """Wrap an optional lookup (the board page next to the API call): any
    upstream failure yields `default` instead of failing the endpoint."""
    def run():
        try:
            return fn()
        except (UpstreamError, BadRequest, NotFound):
            return default
    return run


if __name__ == "__main__":
    # Smoke test: python lever/fetch.py [slug]
    slug = sys.argv[1] if len(sys.argv) > 1 else "spotify"
    rows, region = postings(slug)
    print(f"{slug} ({region}): {len(rows)} postings")
    if rows:
        first = rows[0]
        print(" ", first.get("text"), "|", (first.get("categories") or {}).get("location"), "|", first.get("hostedUrl"))
        one, _ = posting(slug, first["id"], region)
        print("  single:", sorted(one.keys()))
    print("  board page:", len(board_html(slug, region)[0]), "bytes")
