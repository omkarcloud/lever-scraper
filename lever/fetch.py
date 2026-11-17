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

Blocks: none seen. If the pod's egress ever starts answering 403/429, the
sessions move one fallback step down for config.LEVER_BLOCK_COOLDOWN
seconds: since 2026-11-11 first to the gateway
(config.LEVER_FALLBACK_EXIT = "gateway", exit_gateway.py: a fresh gateway address on
nearly every request; the numbers are in config.py's Lever block), and only
when that is refused too to a sticky residential exit
(config.LEVER_FALLBACK_PROXY_COUNTRY). A call the gateway keeps failing
makes its LAST retry on the residential exit, for that call only. When the
cooldown ends the next call is back on the server's own IP. The same shape
as the reed / soundcloud / woolworths transports. Two things never ride the
gateway: a board document over its 10 MB cap (~500+ postings: the call
moves to the residential exit after ONE gateway refusal, _over_gateway_limit)
and a company's own website (site_html: hosts from user input would fill
the gateway's routes).

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
import egress
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from lever import refs
from scraper_utils import fanout, ladder, sessions

IMPERSONATE = "chrome"
TIMEOUT = 40
PAGE_TIMEOUT = 40
FANOUT_WORKERS = 8
REGIONS = ("us", "eu")

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": egress.site_accept_language("lever"),
}
PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": egress.site_accept_language("lever"),
    "upgrade-insecure-requests": "1",
}


class LeverUpstreamError(UpstreamError):
    """Transport failure or 5xx — retryable."""


class LeverBlocked(LeverUpstreamError, Blocked):
    """403 / 429 or an HTML body where JSON was expected — retryable."""


class LeverOverGatewayLimit(LeverUpstreamError):
    """The gateway exit cannot carry this document (over its 10 MB cap) —
    retried at once on the exit behind the gateway (_retrying)."""


class LeverBadRequest(BadRequest):
    """Upstream 400 — the params were rejected. Never retried."""


class LeverNotFound(NotFound):
    """Board / posting does not exist — never retried."""


# ---- sessions ------------------------------------------------------------------
# One session per worker thread (egress.new_session: curl_cffi, or the gateway
# exit's look-alike). Direct egress by default; after a block the sessions
# move down the fallback steps (config.lever_fallback_exits(): the gateway
# exit, then a sticky residential exit) until `_fallback_until`, then go
# back to direct.
_local = threading.local()
_fallback_until = 0.0
_fallback_step = 1              # which fallback exit serves while the cooldown runs (1 = the first)
_fallback_lock = threading.Lock()


def _gateway_step(step):
    """True when fallback step `step` is the gateway exit with another exit
    configured behind it."""
    exits = config.lever_fallback_exits()
    return 0 < step < len(exits) and bool(config.gateway_exit(exits[step - 1]))


def _step():
    """The fallback step a session built NOW rides: 0 = the configured exit
    (the server's own IP), 1, 2 = config.lever_fallback_proxy(step) while
    the cooldown runs; one step past the gateway for the last retry of a
    call (_retrying)."""
    return ladder.current_step(_fallback_step, _fallback_until, _local, _gateway_step)


def _proxy_for_new_session(step=None):
    forced = config.lever_proxy()
    if forced:
        return forced
    step = _step() if step is None else step
    return config.lever_fallback_proxy(step) if step else None


def _session():
    sess = getattr(_local, "session", None)
    step = _step()
    if sess is not None and getattr(sess, "_lever_step", 0) != step:
        # 2026-11-11: a session follows its step. Before, a thread that took
        # the fallback exit during a cooldown stayed on it until that failed.
        _drop_session()
        sess = None
    if sess is None:
        # None (direct), a proxy URL, or the gateway exit ("gateway")
        exit = _proxy_for_new_session(step)
        sess = egress.new_session(exit, impersonate=IMPERSONATE)
        sess._lever_step = step
        sess._lever_gateway = bool(config.gateway_exit(exit))
        _local.session = sess
    return sess


def _off_gateway_session():
    """The session for a host that must never ride the gateway exit: a
    company's own website (site_html). The gateway holds one route per host,
    ~149 for every scraper together, so hosts that come from user input
    stay off it. That is the thread's own session, unless it is a gateway
    one: then a second session on the exit behind the gateway (the
    residential one, what such a page rode during a cooldown before
    2026-11-11), or on the server's own IP when nothing is behind it or
    LEVER_PROXY_COUNTRY itself names the gateway."""
    sess = _session()
    if not getattr(sess, "_lever_gateway", False):
        return sess
    step = sess._lever_step
    plain = getattr(_local, "plain_session", None)
    if plain is not None and getattr(plain, "_lever_step", 0) != step:
        _close(plain)
        plain = None
    if plain is None:
        behind = step + 1 if _gateway_step(step) and not config.lever_proxy() else 0
        plain = egress.new_session(config.lever_fallback_proxy(behind) if behind else None, impersonate=IMPERSONATE)
        plain._lever_step = step
        _local.plain_session = plain
    return plain


def _close(sess):
    sessions.close_quietly(sess)


def _drop_session():
    sess, plain = getattr(_local, "session", None), getattr(_local, "plain_session", None)
    _local.session = _local.plain_session = None
    _close(sess)
    _close(plain)


def _note_block():
    """The exit this thread's session rides was refused: new sessions move
    one fallback step down for a cooldown (own IP -> the gateway exit ->
    residential; on the last step they stay there, each on a fresh exit).
    No-op when no fallback is configured."""
    global _fallback_until, _fallback_step
    exits = config.lever_fallback_exits()
    if not exits:
        return
    own = getattr(getattr(_local, "session", None), "_lever_step", _step())
    if getattr(_local, "past_gateway", False) and own > (_fallback_step if time.time() < _fallback_until else 0):
        # the exit borrowed behind the gateway for this call's last retry
        # (_retrying) was refused: no verdict on the gateway, the step stays
        return
    step = min(own + 1, len(exits))
    with _fallback_lock:
        if time.time() >= _fallback_until or step > _fallback_step:
            _fallback_step = step
        _fallback_until = time.time() + config.LEVER_BLOCK_COOLDOWN
    if len(exits) > 1:      # how often each step is refused is the number to watch
        print(f"lever: exit refused, sessions on fallback step {_fallback_step} "
              f"({exits[_fallback_step - 1]}) for {config.LEVER_BLOCK_COOLDOWN} s")


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
    """Run fn() under the shared retry policy, each retry on a fresh session
    / exit. While the sessions are on the gateway exit step the LAST retry of
    a call rides the next step (the residential exit), for that call only:
    a gateway failure then costs latency, not the call."""
    last = None
    too_big = False
    _local.past_gateway = False
    try:
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                return fn()
            except LeverUpstreamError as e:      # includes LeverBlocked
                last = e
                too_big = too_big or isinstance(e, LeverOverGatewayLimit)
                _drop_session()
            if attempt < config.MAX_RETRIES:
                # read by _step() for the session built next; a document the
                # gateway cannot carry leaves it at once, not on the last retry
                _local.past_gateway = too_big or attempt + 1 == config.MAX_RETRIES
                time.sleep(config.RETRY_BACKOFF * attempt)
        raise last
    finally:
        _local.past_gateway = False     # _session() rebuilds on the thread's own step for the next call


def _over_gateway_limit(error):
    """True for the gateway's own HTTP 500 (exit_gateway.GatewayError "
    gateway 500 InternalServerErrorException"): what it answers when the
    target's body is over its 10 MB cap. Lever sends a board uncompressed,
    ~19 KB a posting, so a board of ~500+ postings is over it (measured
    2026-11-11: 16 of 16 requests for 7 boards of 857-4,651 postings, the 6
    measured direct 15-55 MB, each refused in 1-18 s; a 5.9 MB board of 304
    passed)."""
    return type(error).__name__ == "GatewayError" and "gateway 500" in str(error)


def get_json(url, params=None, *, label=None):
    label = label or url
    query = {k: v for k, v in (params or {}).items() if v is not None}

    def once():
        try:
            resp = _session().get(url, params=query or None, headers=API_HEADERS, timeout=TIMEOUT,
                                  allow_redirects=True)
        except Exception as e:
            if _over_gateway_limit(e):
                raise LeverOverGatewayLimit(f"request failed: {type(e).__name__}: {e}")
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
    failure is None — the lookup just moves on to slug guesses. Never
    through the gateway exit (_off_gateway_session)."""
    try:
        resp = _off_gateway_session().get(url, headers=PAGE_HEADERS, timeout=timeout, allow_redirects=True)
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
    return fanout.on_pool_caller_first(_fanout, fns)


def soft(fn, default=None):
    """Wrap an optional lookup (the board page next to the API call): any
    upstream failure yields `default` instead of failing the endpoint."""
    return fanout.soft(fn, default, (UpstreamError, BadRequest, NotFound))


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
