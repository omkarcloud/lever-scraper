"""/lever/companies/* — one Lever job board: its profile with hiring stats
and filter facets, its open jobs with filters, the directory of known
boards, and the resolver that turns a company name / website into its slug.
"""
import re

from lever import filters
from lever import index
from lever import parsers as P
from lever import refs
from lever.fetch import postings, probe, run_parallel, site_html, soft

_LEVER_LINK_RE = re.compile(r"https?://(?:jobs|api)\.(eu\.)?lever\.co/(?:v0/postings/)?([A-Za-z0-9._-]{1,80})", re.I)
CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/jobs/", "/join-us", "/company/careers", "/about/careers")
MAX_SITE_PAGES = 4


def _company(slug, region, profile, rows):
    """The company block of a response: directory + board page facts."""
    index.learn(slug, region, profile, open_jobs=len(rows), countries=P.countries_of(rows))
    row = index.company(slug) or {}
    return {
        "slug": slug,
        "name": row.get("name"),
        "link": refs.board_link(slug, region),
        "api_link": refs.api_link(slug, region),
        "website": row.get("website"),
        "logo": row.get("logo"),
        "description": row.get("description"),
        "social_image": row.get("social_image"),
        "region": region,
        "open_jobs": len(rows),
        "countries": P.countries_of(rows),
    }


def _board(slug, with_profile):
    rows, region = postings(slug)
    profile = None
    if with_profile or not (index.company(slug) or {}).get("name"):
        profile = index.read_profile(slug, region, rows)
    return rows, region, profile


def details(company):
    """Profile + hiring stats + facets for one board."""
    rows, region, profile = _board(company, with_profile=True)
    block = _company(company, region, profile, rows)
    job_rows = P.jobs(rows, company, region, P.company_ref(company, region, block["name"]))
    index.store(company, region, job_rows)
    return {"company": block, "stats": P.stats(job_rows), "facets": P.facets(job_rows)}


def jobs(company, query=None, team=None, department=None, location=None, commitment=None,
         workplace_type=None, country=None, posted_since=None, has_salary=None, sort="newest",
         page=1, per_page=20, include_description=False):
    """Every open job of one board, filtered and paged locally (Lever's own
    filters are exact-match and case-sensitive; its API has no search)."""
    rows, region, profile = _board(company, with_profile=False)
    block = _company(company, region, profile, rows)
    ref = P.company_ref(company, region, block["name"])
    all_jobs = P.jobs(rows, company, region, ref)
    index.store(company, region, all_jobs)
    if include_description:
        all_jobs = P.jobs(rows, company, region, ref, include_description=True)
    matched = filters.apply(all_jobs, query, team, department, location, commitment, workplace_type,
                            country, posted_since, has_salary)
    page_rows, pagination = filters.page(filters.sort(matched, sort), page, per_page)
    return {
        "company": block,
        "filters": filters.echo(query=query, team=team, department=department, location=location,
                                commitment=commitment, workplace_type=workplace_type, country=country,
                                posted_since=posted_since, has_salary=has_salary, sort=sort),
        "pagination": pagination,
        "jobs": page_rows,
    }


# ---- directory ---------------------------------------------------------------------

def _directory_row(row):
    return {
        "slug": row["slug"],
        "name": row.get("name"),
        "link": refs.board_link(row["slug"], row.get("region")),
        "website": row.get("website"),
        "logo": row.get("logo"),
        "region": row.get("region"),
        "open_jobs": row.get("open_jobs"),
        "countries": row.get("countries") or [],
    }


def search(query=None, country=None, min_open_jobs=None, sort="open_jobs", page=1, per_page=20):
    """The known Lever boards (the bundled directory + every board resolved
    at runtime), filtered by name / slug, country and open-job count."""
    index.touch()
    rows = index.companies()
    needle = (query or "").lower()
    out = []
    for row in rows:
        if needle and not (needle in (row.get("name") or "").lower() or needle in row["slug"]):
            continue
        if country and country not in (row.get("countries") or []):
            continue
        if min_open_jobs is not None and (row.get("open_jobs") or 0) < min_open_jobs:
            continue
        out.append(row)
    if sort == "name":
        out.sort(key=lambda r: ((r.get("name") or r["slug"]).lower(), r["slug"]))
    else:
        out.sort(key=lambda r: (-(r.get("open_jobs") or 0), (r.get("name") or r["slug"]).lower()))
    page_rows, pagination = filters.page(out, page, per_page)
    return {
        "query": query,
        "index": index.coverage(),
        "pagination": pagination,
        "companies": [_directory_row(r) for r in page_rows],
    }


# ---- resolve ------------------------------------------------------------------------

def _found(slug, region, open_jobs, how, tried):
    if not (index.company(slug) or {}).get("name"):
        rows, _ = soft(lambda: postings(slug, region), default=([], region))()
        index.learn(slug, region, index.read_profile(slug, region, rows), open_jobs=open_jobs,
                    countries=P.countries_of(rows))
    else:
        index.learn(slug, region, None, open_jobs=open_jobs)
    row = index.company(slug) or {"slug": slug, "region": region}
    return {
        "is_found": True,
        "matched_by": how,
        "company": _directory_row(dict(row, open_jobs=open_jobs)),
        "slugs_tried": tried,
    }


def _probe_all(candidates, tried):
    """Probe candidate slugs in parallel; -> (slug, region, count) of the
    first hit in candidate order, else None."""
    candidates = [c for c in candidates if c and c not in tried]
    if not candidates:
        return None
    results = run_parallel([(lambda s=s: soft(lambda: probe(s), default=None)()) for s in candidates])
    tried.extend(candidates)
    for slug, hit in zip(candidates, results):
        if hit:
            return slug, hit[0], hit[1]
    return None


def _links_in(page):
    out = []
    for region, slug in _LEVER_LINK_RE.findall(page or ""):
        slug = slug.lower()
        if refs.SLUG_RE.match(slug) and slug not in refs.NOT_SLUGS and slug not in out:
            out.append(slug)
    return out


def resolve(company):
    """Which Lever board (if any) a company uses. A Lever link answers
    directly; a website is read (homepage + the usual careers paths) for a
    jobs.lever.co embed; a name is matched against the directory; and in
    every case the obvious slug spellings are probed against the API."""
    index.touch()
    kind, value = company["kind"], company["value"]
    tried = []
    if kind == "lever":
        hit = soft(lambda: probe(value), default=None)()
        tried.append(value)
        if hit:
            return _found(value, hit[0], hit[1], "lever_link", tried)
        return {"is_found": False, "matched_by": None, "company": None, "slugs_tried": tried}

    guesses = []
    if kind == "site":
        host = re.sub(r"^https?://", "", value).split("/")[0]
        base = f"https://{host}"
        pages = [value] if value.rstrip("/") != base else []
        pages += [base + path for path in CAREERS_PATHS]
        embedded = []
        for url in pages[:MAX_SITE_PAGES + 1]:
            page = site_html(url)
            if page:
                embedded = _links_in(page)
                if embedded:
                    break
        hit = _probe_all(embedded, tried)
        if hit:
            return _found(hit[0], hit[1], hit[2], "website_embed", tried)
        guesses = refs.slug_guesses(host)
    else:
        needle = value.lower()
        rows = index.companies()
        exact = [r for r in rows if (r.get("name") or "").lower() == needle or r["slug"] == needle.replace(" ", "")]
        partial = [r for r in rows if needle in (r.get("name") or "").lower()]
        for row in exact + [r for r in partial if r not in exact]:
            hit = soft(lambda s=row["slug"]: probe(s), default=None)()
            tried.append(row["slug"])
            if hit:
                return _found(row["slug"], hit[0], hit[1], "directory_name", tried)
        guesses = refs.slug_guesses(value)

    hit = _probe_all(guesses, tried)
    if hit:
        return _found(hit[0], hit[1], hit[2], "slug_guess", tried)
    return {"is_found": False, "matched_by": None, "company": None, "slugs_tried": tried}
