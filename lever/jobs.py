"""/lever/jobs/* — one posting in full, its application form, and the
cross-company search / new-postings feed over the index (lever/index.py).
"""
from cache import DontCache
from lever import filters
from lever import index
from lever import parsers as P
from lever import refs
from lever.fetch import LeverNotFound, apply_html, posting, posting_html, run_parallel, soft


def _resolve(job, company):
    """{"slug", "id"} (+ an optional `company`) -> (slug, posting id)."""
    slug = job.get("slug") or company
    if slug and company and job.get("slug") and job["slug"] != company:
        raise ValueError(f"job link names the '{job['slug']}' board but company is '{company}'")
    if not slug:
        hit = index.find_job(job["id"])
        if not hit:
            raise ValueError("a bare posting id needs its board: pass company=<slug>, "
                             "job=<slug>/<id> or the jobs.lever.co posting link")
        slug = hit[0]
    return slug, job["id"]


def details(job, company=None):
    """The full posting: every description block, salary, locations, and
    the hiring organization from the posting page's JSON-LD."""
    slug, job_id = _resolve(job, company)
    (raw, region), page = run_parallel([
        lambda: posting(slug, job_id),
        soft(lambda: posting_html(slug, job_id)[0], default=None),
    ])
    org = P.company_from_jsonld(P.jsonld(page)) if page else {"name": None, "logo": None}
    ref = index.company_ref(slug, region)
    ref["name"] = ref["name"] or org["name"]
    row = P.job(raw, slug, region, ref, include_description=True)
    row["company"]["logo"] = org["logo"] or (index.company(slug) or {}).get("logo")
    if org["name"] or org["logo"]:
        index.learn(slug, region, {"name": org["name"], "logo": org["logo"]})
    return {"job": row}


def application_form(job, company=None):
    """What the posting's application form asks: the standard contact
    fields, the location picker, the company's custom question cards and
    its demographic surveys, plus whether a resume / captcha is required."""
    slug, job_id = _resolve(job, company)
    page, region = apply_html(slug, job_id)
    form = P.application_form(page)
    ref = index.company_ref(slug, region)
    title = P.text(P.metas(page).get("twitter:title") or P.page_title(page))
    if title and ref.get("name") and title.lower().startswith(ref["name"].lower() + " - "):
        title = title[len(ref["name"]) + 3:]
    if not form["fields"] and not form["custom_questions"]:
        # a closed posting's /apply answers 200 with the "no longer accepting"
        # shell; nothing to report
        raise LeverNotFound(f"job '{job_id}' on '{slug}' has no application form (closed?)")
    return {
        "job": {
            "id": job_id,
            "title": title,
            "link": refs.job_link(slug, job_id, region),
            "apply_link": refs.apply_link(slug, job_id, region),
            "company": ref,
        },
        **form,
    }


def search(query=None, companies=None, team=None, department=None, location=None, commitment=None,
           workplace_type=None, country=None, posted_since=None, has_salary=None, sort="newest",
           page=1, per_page=20):
    """Jobs across Lever boards. With `companies` the named boards are read
    (fresh) and searched; without, every board in the index."""
    index.touch()
    if companies:
        index.ensure(companies)
        pool = index.jobs_of(companies)
    else:
        pool = index.jobs_of()
    matched = filters.apply(pool, query, team, department, location, commitment, workplace_type,
                            country, posted_since, has_salary)
    page_rows, pagination = filters.page(filters.sort(matched, sort), page, per_page)
    out = {
        "filters": filters.echo(query=query, companies=companies, team=team, department=department,
                                location=location, commitment=commitment, workplace_type=workplace_type,
                                country=country, posted_since=posted_since, has_salary=has_salary, sort=sort),
    }
    if not companies:
        out["index"] = index.coverage()
    out["pagination"] = pagination
    out["jobs"] = page_rows
    return _cacheable(out)


def _cacheable(out):
    """An untargeted answer from a half-warm index (start-up, or boards
    still failing) is served but not cached: the next call sees more boards."""
    if "index" in out and not out["index"]["is_complete"]:
        return DontCache(out)
    return out


def new(since, companies=None, query=None, team=None, department=None, location=None, commitment=None,
        workplace_type=None, country=None, posted_since=None, has_salary=None, sort="newest", page=1, per_page=20):
    """Postings published since a moment (default: the last 7 days), newest
    first — the monitoring feed. `since` takes '24h' / '7d' / a date."""
    floor = since if not posted_since or posted_since < since else posted_since
    out = search(query, companies, team, department, location, commitment, workplace_type, country,
                 floor, has_salary, sort, page, per_page)
    out = out.data if isinstance(out, DontCache) else out
    out["filters"]["since"] = out["filters"].pop("posted_since", None)
    return _cacheable({"since": floor, **out})
