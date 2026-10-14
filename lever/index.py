"""The cross-company index: Lever has no directory of its customers and no
search across boards, so /lever/jobs/search, /lever/jobs/new and
/lever/companies/search run over this in-process index.

  directory   lever/directory.json (built by lever/tools/build_directory.py:
              slugs harvested from Common Crawl + seed lists, validated
              against the API, with the board's name / logo / website) plus
              every board a caller names at runtime (`learn`): a company
              someone asks for once is searchable for everyone afterwards.

  boards      per slug, the board's postings as slim normalized jobs (no
              description — the details endpoint fetches that live), read
              through lever/fetch.py and kept config.LEVER_INDEX_TTL seconds.

  refresher   a daemon thread (started by run.py when the scraper is
              active) that warms every directory board at start-up
              (config.LEVER_INDEX_WARM_AT_START) and then re-reads boards as
              they go stale, LEVER_INDEX_WORKERS at a time — but only while
              a cross-company endpoint was used in the last
              LEVER_INDEX_IDLE_AFTER seconds, so an idle service fetches
              nothing (the trustpilot rule).

A cross-company request never waits for the whole directory: it searches
the boards loaded so far and reports index coverage (`index` block). Boards
the caller names explicitly (`companies=`) ARE fetched synchronously when
missing or stale, so a targeted search is always current.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
from lever import parsers as P
from lever import refs
from lever.fetch import LeverNotFound, UpstreamError, board_html, posting_html, postings, remember_region, soft

DIRECTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "directory.json")

_lock = threading.RLock()
_companies = {}          # slug -> directory row (name, logo, website, region, open_jobs, countries, ...)
_boards = {}             # slug -> {"fetched_at": epoch, "region": str, "jobs": [slim job], "error": str | None}
_inflight = {}           # slug -> threading.Event while a fetch runs
_last_used = 0.0         # epoch of the last cross-company request
_refresher_started = False
_directory_loaded = False
_built_at = None


# ---- directory ------------------------------------------------------------------

def _row(slug, region=None, name=None, logo=None, website=None, open_jobs=None, countries=None,
         description=None, social_image=None, source="directory"):
    return {"slug": slug, "region": region or "us", "name": name, "logo": logo, "website": website,
            "description": description, "social_image": social_image,
            "open_jobs": open_jobs, "countries": list(countries or []), "source": source}


def load_directory(path=DIRECTORY_PATH):
    global _directory_loaded, _built_at
    with _lock:
        if _directory_loaded:
            return
        _directory_loaded = True
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            print(f"lever: could not read {path}: {e}")
            return
        _built_at = doc.get("built_at")
        for raw in doc.get("companies") or []:
            slug = (raw.get("slug") or "").strip().lower()
            if not slug or not refs.SLUG_RE.match(slug):
                continue
            _companies[slug] = _row(slug, raw.get("region"), raw.get("name"), raw.get("logo"),
                                    raw.get("website"), raw.get("open_jobs"), raw.get("countries"),
                                    raw.get("description"), raw.get("social_image"))
            remember_region(slug, raw.get("region"))


def company(slug):
    load_directory()
    with _lock:
        row = _companies.get(slug)
        return dict(row) if row else None


def companies():
    load_directory()
    with _lock:
        return [dict(r) for r in _companies.values()]


def learn(slug, region, profile=None, open_jobs=None, countries=None):
    """Record a board a caller resolved (name / logo / website from its
    board page when given) so the cross-company endpoints cover it too."""
    load_directory()
    with _lock:
        row = _companies.get(slug) or _row(slug, region, source="runtime")
        row["region"] = region or row["region"]
        for key in ("name", "logo", "website", "description", "social_image"):
            value = (profile or {}).get(key)
            if value:
                row[key] = value
        if open_jobs is not None:
            row["open_jobs"] = open_jobs
        if countries is not None:
            row["countries"] = list(countries)
        _companies[slug] = row
    remember_region(slug, region)


def read_profile(slug, region, rows=None):
    """The company's name / logo / website: from the hosted board page, or
    — for boards whose hosted page is switched off (the company embeds the
    API in its own careers site; jobs.lever.co/<slug> answers 404) — from
    the first posting page's JSON-LD. None when neither is readable."""
    page, _ = soft(lambda: board_html(slug, region), default=(None, region))()
    if page:
        return P.company_from_board_html(page, slug, region)
    first = next((r.get("id") for r in rows or [] if isinstance(r, dict) and r.get("id")), None)
    if not first:
        return None
    page, _ = soft(lambda: posting_html(slug, first, region), default=(None, region))()
    org = P.company_from_jsonld(P.jsonld(page)) if page else {}
    return {"name": org.get("name"), "logo": org.get("logo")} if org.get("name") or org.get("logo") else None


def company_ref(slug, region=None):
    row = company(slug)
    return P.company_ref(slug, region or (row or {}).get("region") or "us", (row or {}).get("name"))


# ---- boards -------------------------------------------------------------------------

def _slim(raw_rows, slug, region):
    return P.jobs(raw_rows, slug, region, company_ref(slug, region))


def _fetch_board(slug, with_profile=False):
    """Read one board into the index. Returns the entry. A board that is
    gone (404 on both shards) is recorded as such and dropped from the
    directory; a transport failure keeps the previous rows."""
    event = threading.Event()
    with _lock:
        running = _inflight.get(slug)
        if running is not None:
            event = running
        else:
            _inflight[slug] = event
            running = None
    if running is not None:
        event.wait(TIMEOUT_WAIT)
        with _lock:
            return _boards.get(slug)
    try:
        try:
            rows, region = postings(slug)
        except LeverNotFound:
            with _lock:
                _boards[slug] = {"fetched_at": time.time(), "region": None, "jobs": [], "error": "not a Lever board"}
                _companies.pop(slug, None)
                return _boards[slug]
        except UpstreamError as e:
            with _lock:
                previous = _boards.get(slug)
                if previous:
                    previous["error"] = str(e)
                    previous["fetched_at"] = time.time() - config.LEVER_INDEX_TTL + RETRY_AFTER
                    return previous
                _boards[slug] = {"fetched_at": time.time() - config.LEVER_INDEX_TTL + RETRY_AFTER,
                                 "region": None, "jobs": [], "error": str(e)}
                return _boards[slug]
        profile = None
        if with_profile or not (company(slug) or {}).get("name"):
            profile = read_profile(slug, region, rows)
        learn(slug, region, profile, open_jobs=len(rows), countries=P.countries_of(rows))
        entry = {"fetched_at": time.time(), "region": region, "jobs": _slim(rows, slug, region), "error": None}
        with _lock:
            _boards[slug] = entry
        return entry
    finally:
        with _lock:
            _inflight.pop(slug, None)
        event.set()


TIMEOUT_WAIT = 120
RETRY_AFTER = 300     # seconds before a board whose read failed is tried again


def _is_fresh(entry, now=None):
    return bool(entry) and (now or time.time()) - entry["fetched_at"] < config.LEVER_INDEX_TTL


def board(slug, max_age=None):
    """The board's slim jobs, fetched if missing or older than `max_age`
    (default: the index TTL). Raises LeverNotFound for a board that does
    not exist."""
    with _lock:
        entry = _boards.get(slug)
    ttl = config.LEVER_INDEX_TTL if max_age is None else max_age
    if not entry or time.time() - entry["fetched_at"] >= ttl:
        entry = _fetch_board(slug)
    if entry.get("region") is None and entry.get("error") == "not a Lever board":
        raise LeverNotFound(f"company: '{slug}' is not a Lever job board (no board on jobs.lever.co or jobs.eu.lever.co)")
    return entry


def store(slug, region, job_rows):
    """Hand the index a board's rows an endpoint already fetched (no second
    read): the slim copy, minus descriptions."""
    slim = [{k: v for k, v in r.items() if k != "description"} for r in job_rows]
    with _lock:
        _boards[slug] = {"fetched_at": time.time(), "region": region, "jobs": slim, "error": None}


def ensure(slugs, workers=None):
    """Fetch the named boards that are missing or stale, in parallel; the
    search then reads them from the index. Missing boards raise
    LeverNotFound (with every slug that failed)."""
    missing = []
    for slug in slugs:
        with _lock:
            entry = _boards.get(slug)
        if not _is_fresh(entry):
            missing.append(slug)
    if missing:
        with ThreadPoolExecutor(max_workers=min(len(missing), workers or config.LEVER_INDEX_WORKERS * 2)) as pool:
            list(pool.map(lambda s: _fetch_board(s), missing))
    unknown = []
    for slug in slugs:
        with _lock:
            entry = _boards.get(slug)
        if entry and entry.get("region") is None and entry.get("error") == "not a Lever board":
            unknown.append(slug)
    if unknown:
        raise LeverNotFound(f"not Lever job boards: {', '.join(unknown)}")


def touch():
    global _last_used
    _last_used = time.time()


def jobs_of(slugs=None):
    """Every indexed job of the named boards (default: all loaded boards)."""
    with _lock:
        if slugs is None:
            entries = list(_boards.values())
        else:
            entries = [_boards[s] for s in slugs if s in _boards]
        out = []
        for entry in entries:
            out.extend(entry["jobs"])
        return out


def find_job(job_id):
    """-> (slug, region) of an indexed posting id, or None."""
    with _lock:
        for slug, entry in _boards.items():
            for row in entry["jobs"]:
                if row.get("id") == job_id:
                    return slug, entry["region"]
    return None


def coverage():
    """The `index` block of cross-company responses."""
    load_directory()
    now = time.time()
    with _lock:
        total = len(_companies)
        loaded = [e for s, e in _boards.items() if e.get("region")]
        fresh = sum(1 for e in loaded if _is_fresh(e, now))
        newest = max((e["fetched_at"] for e in loaded), default=None)
    return {
        "companies_total": total,
        "companies_indexed": len(loaded),
        "companies_fresh": fresh,
        "jobs_indexed": sum(len(e["jobs"]) for e in loaded),
        "is_complete": len(loaded) >= total and total > 0,
        "refreshed_at": datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None,
        "directory_built_at": _built_at,
    }


# ---- refresher ---------------------------------------------------------------------

def _stale_slugs():
    now = time.time()
    with _lock:
        slugs = list(_companies)
        return [s for s in slugs if not _is_fresh(_boards.get(s), now)]


def _refresh_one(slug):
    """_fetch_board for the refresher: one board failing for a reason the
    fetch does not record itself (an upstream 400, a parser surprise) must
    not abort the pass — or, at warm-up, kill the refresher thread."""
    try:
        _fetch_board(slug)
    except Exception as e:
        print(f"lever: index refresh of '{slug}' failed: {type(e).__name__}: {e}")


def refresh_pass(limit=None, workers=None):
    """Read every stale directory board (a bounded batch when `limit`)."""
    stale = _stale_slugs()
    if limit:
        stale = stale[:limit]
    if not stale:
        return 0
    with ThreadPoolExecutor(max_workers=workers or config.LEVER_INDEX_WORKERS) as pool:
        list(pool.map(_refresh_one, stale))
    return len(stale)


def _refresher_loop(warm):
    if warm:
        started = time.time()
        n = refresh_pass()
        print(f"lever: index warmed — {n} boards read in {time.time() - started:.0f}s; {coverage()}")
    while True:
        time.sleep(config.LEVER_INDEX_REFRESH_POLL)
        if time.time() - _last_used > config.LEVER_INDEX_IDLE_AFTER:
            continue     # nobody is searching across companies: fetch nothing
        try:
            refresh_pass()
        except Exception as e:
            print(f"lever: index refresh failed: {type(e).__name__}: {e}")


def start_refresher(warm=None):
    """Start the background refresher once (idempotent)."""
    global _refresher_started
    load_directory()
    with _lock:
        if _refresher_started:
            return
        _refresher_started = True
    warm = config.LEVER_INDEX_WARM_AT_START if warm is None else warm
    threading.Thread(target=_refresher_loop, args=(warm,), daemon=True, name="lever-index-refresh").start()
