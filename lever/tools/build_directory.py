"""Build lever/directory.json — the seed index of Lever-hosted job boards.

Lever has no public directory of the companies on it, so the cross-company
endpoints (/lever/jobs/search, /lever/companies/search) fan out over a list
we maintain. This tool (re)builds that list:

  1. harvest candidate slugs
       * the Common Crawl URL index for jobs.lever.co/* and
         jobs.eu.lever.co/* over the last N crawls (free, no key)
       * optional seed files: a CSV with a `token` column (the
         kalebconfer job-board-directory export) and/or a plain text file
         with one slug per line
       * the slugs already in the current directory.json (a rebuild never
         forgets a board that is still live)
  2. validate every slug against the postings API — US host first, then
     the EU host on 404 — keeping only boards that answer 200
  3. read each live board's hosted page for its display name, logo and
     website, and count its open postings (+ the countries they are in)

Usage (from ./scrapers):

    python lever/tools/build_directory.py --out lever/directory.json \
        --csv /path/to/lever.csv --slugs /path/to/extra_slugs.txt \
        --crawls 12 --workers 16

Writes {"built_at", "count", "companies": [...]} sorted by open_jobs desc.
Idempotent: safe to re-run; a slug that 404s on both hosts is dropped.
"""
import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from curl_cffi import requests as curl_requests  # noqa: E402

from lever import parsers as P  # noqa: E402
from lever.refs import SLUG_RE, NOT_SLUGS  # noqa: E402

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_INDEX = "https://index.commoncrawl.org/{crawl}-index"
API = {"us": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}
BOARD = {"us": "https://jobs.lever.co", "eu": "https://jobs.eu.lever.co"}
TIMEOUT = 40

_print_lock = threading.Lock()


def log(*args):
    with _print_lock:
        print(*args, flush=True)


def _session():
    return curl_requests.Session(impersonate="chrome")


# ---- harvest ---------------------------------------------------------------------

def commoncrawl_crawls(limit):
    sess = _session()
    for attempt in range(3):
        try:
            resp = sess.get(CC_COLLINFO, timeout=TIMEOUT)
            if resp.status_code == 200:
                return [c["id"] for c in resp.json()][:limit]
        except Exception as e:
            log(f"collinfo attempt {attempt + 1} failed: {e}")
        time.sleep(3)
    return []


def commoncrawl_slugs(crawl, host, sess):
    """Every first path segment under `host` in one crawl's URL index."""
    found = set()
    base = CC_INDEX.format(crawl=crawl)
    pages = 1
    try:
        resp = sess.get(base, params={"url": f"{host}/*", "output": "json", "showNumPages": "true"}, timeout=TIMEOUT)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            pages = int(resp.json().get("pages") or 1)
    except Exception as e:
        log(f"  {crawl} {host}: page count failed ({e}); assuming 1 page")
    for page in range(pages):
        for attempt in range(3):
            try:
                resp = sess.get(base, params={"url": f"{host}/*", "output": "json", "fl": "url", "page": page},
                                timeout=TIMEOUT)
            except Exception as e:
                log(f"  {crawl} {host} page {page} attempt {attempt + 1}: {e}")
                time.sleep(2)
                continue
            if resp.status_code != 200:
                log(f"  {crawl} {host} page {page}: HTTP {resp.status_code}")
                time.sleep(2)
                continue
            for line in resp.text.splitlines():
                try:
                    url = json.loads(line).get("url") or ""
                except ValueError:
                    continue
                slug = slug_of_url(url)
                if slug:
                    found.add(slug)
            break
    return found


def slug_of_url(url):
    path = urlparse(url).path
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    slug = segments[0].strip().lower()
    if slug in NOT_SLUGS or not SLUG_RE.match(slug):
        return None
    return slug


def csv_slugs(path):
    out = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            token = (row.get("token") or row.get("slug") or "").strip().lower()
            if token and SLUG_RE.match(token):
                out.add(token)
    return out


def text_slugs(path):
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            slug = slug_of_url(line.strip()) if "://" in line else line.strip().lower()
            if slug and SLUG_RE.match(slug) and slug not in NOT_SLUGS:
                out.add(slug)
    return out


def existing_slugs(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return {c["slug"] for c in doc.get("companies") or [] if c.get("slug")}


# ---- validate ---------------------------------------------------------------------

def check_board(slug):
    """-> a directory row or None (404 on both hosts / transport failure)."""
    sess = _session()
    for region in ("us", "eu"):
        url = f"{API[region]}/{slug}"
        try:
            resp = sess.get(url, params={"mode": "json"}, timeout=TIMEOUT)
        except Exception as e:
            log(f"  {slug}: {region} transport failure ({e})")
            continue
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            log(f"  {slug}: {region} HTTP {resp.status_code}")
            continue
        try:
            postings = resp.json()
        except Exception:
            continue
        if not isinstance(postings, list):
            continue
        row = {"slug": slug, "region": region, "name": None, "logo": None, "website": None,
               "open_jobs": len(postings), "countries": P.countries_of(postings),
               "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        try:
            page = sess.get(f"{BOARD[region]}/{slug}", timeout=TIMEOUT)
            if page.status_code == 200:
                row.update(P.company_from_board_html(page.text, slug, region))
            elif postings:
                # hosted board switched off: the posting page still carries JSON-LD
                page = sess.get(f"{BOARD[region]}/{slug}/{postings[0].get('id')}", timeout=TIMEOUT)
                if page.status_code == 200:
                    org = P.company_from_jsonld(P.jsonld(page.text))
                    row.update({"name": org.get("name"), "logo": org.get("logo")})
        except Exception as e:
            log(f"  {slug}: board page failed ({e})")
        return row
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "directory.json"))
    ap.add_argument("--csv", action="append", default=[], help="CSV seed with a `token` column")
    ap.add_argument("--slugs", action="append", default=[], help="text seed: one slug or board URL per line")
    ap.add_argument("--crawls", type=int, default=12, help="how many recent Common Crawl indexes to harvest")
    ap.add_argument("--no-commoncrawl", action="store_true")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--candidates-out", default=None, help="also dump the raw candidate slug list here")
    args = ap.parse_args()

    candidates = existing_slugs(args.out)
    log(f"{len(candidates)} slugs carried over from {args.out}")
    for path in args.csv:
        found = csv_slugs(path)
        log(f"{len(found)} slugs from {path}")
        candidates |= found
    for path in args.slugs:
        found = text_slugs(path)
        log(f"{len(found)} slugs from {path}")
        candidates |= found

    if not args.no_commoncrawl:
        crawls = commoncrawl_crawls(args.crawls)
        log(f"harvesting Common Crawl: {crawls}")
        sess = _session()
        for crawl in crawls:
            for host in ("jobs.lever.co", "jobs.eu.lever.co"):
                found = commoncrawl_slugs(crawl, host, sess)
                log(f"  {crawl} {host}: {len(found)} slugs")
                candidates |= found

    candidates = sorted(candidates)
    log(f"{len(candidates)} candidate slugs to validate")
    if args.candidates_out:
        with open(args.candidates_out, "w", encoding="utf-8") as f:
            f.write("\n".join(candidates) + "\n")

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_board, slug): slug for slug in candidates}
        for future in as_completed(futures):
            done += 1
            row = future.result()
            if row:
                rows.append(row)
            if done % 100 == 0:
                log(f"  validated {done}/{len(candidates)} ({len(rows)} live)")

    rows.sort(key=lambda r: (-(r["open_jobs"] or 0), r["slug"]))
    doc = {"built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "count": len(rows), "companies": rows}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    log(f"wrote {len(rows)} live boards to {args.out} "
        f"({sum(1 for r in rows if r['region'] == 'eu')} on the EU host, "
        f"{sum(r['open_jobs'] for r in rows)} open jobs)")


if __name__ == "__main__":
    main()
