"""Live endpoint smoke tests: one call per /lever/* route against a running
service, with example values proven to return data (2026-10-14). The
listing tooling reads each route's FIRST call from this file's AST as its
working example, so the values stay literals.

Skipped unless LEVER_BASE points at a running service:

    ONLY_SCRAPER=lever python run.py
    LEVER_BASE=http://127.0.0.1:6002 python -m pytest lever/test_endpoints.py -q
"""
import os

import pytest

BASE = os.environ.get("LEVER_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="set LEVER_BASE to run live endpoint tests")


def call(path, status=200, **params):
    from curl_cffi import requests
    resp = requests.get(BASE + path, params=params, timeout=180)
    assert resp.status_code == status, f"{path} {params} -> {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body, f"{path} returned an empty body"
    return body


def test_company_details():
    body = call("/lever/companies/details", company="spotify")
    company = body["company"]
    assert company["name"] == "Spotify" and company["logo"] and company["website"] and company["region"] == "us"
    assert company["open_jobs"] > 20 and "GB" in company["countries"]
    assert body["stats"]["open_jobs"] == company["open_jobs"] and body["stats"]["newest_posted_at"]
    assert body["facets"]["teams"] and body["facets"]["locations"] and body["facets"]["workplace_types"]
    eu = call("/lever/companies/details", company="https://jobs.eu.lever.co/aavelabs")["company"]
    assert eu["region"] == "eu" and eu["link"] == "https://jobs.eu.lever.co/aavelabs"


def test_company_jobs():
    page = call("/lever/companies/jobs", company="spotify")
    assert page["count"] > 20 and page["per_page"] == 20 and page["jobs"][0]["posted_at"]
    assert page["company"]["name"] == "Spotify" and "description" not in page["jobs"][0]
    filtered = call("/lever/companies/jobs", company="https://jobs.lever.co/spotify", query="engineer",
                    workplace_type="hybrid", sort="oldest", per_page=5)
    assert 0 < filtered["count"] < page["count"] and len(filtered["jobs"]) <= 5
    assert all("engineer" in j["title"].lower() and j["workplace_type"] == "hybrid" for j in filtered["jobs"])
    described = call("/lever/companies/jobs", company="spotify", per_page=1, include_description="true")
    assert described["jobs"][0]["description"]["full_text"] and described["jobs"][0]["description"]["sections"]
    assert call("/lever/companies/jobs", company="spotify", page=2, per_page=10)["current_page"] == 2
    assert call("/lever/companies/jobs", company="zoox", has_salary="true")["jobs"][0]["salary"]["currency"] == "USD"


def test_company_directory():
    found = call("/lever/companies/search", query="spotify")
    assert found["count"] >= 1 and found["companies"][0]["slug"] == "spotify" and found["index"]["companies_total"] > 100
    assert call("/lever/companies/search", min_open_jobs=100, sort="name")["companies"]
    assert call("/lever/companies/search", country="SE")["companies"]


def test_company_resolve():
    by_name = call("/lever/companies/resolve", company="Palantir")
    assert by_name["is_found"] and by_name["company"]["slug"] == "palantir"
    assert call("/lever/companies/resolve", company="https://www.wealthfront.com/careers")["company"]["slug"] == "wealthfront"
    assert call("/lever/companies/resolve", company="spotify.com")["is_found"]
    assert call("/lever/companies/resolve", company="https://jobs.eu.lever.co/aavelabs")["company"]["name"] == "Aave Labs"
    miss = call("/lever/companies/resolve", company="nonexistent-company-xyz")
    assert miss["is_found"] is False and miss["slugs_tried"]


def test_job_details():
    body = call("/lever/jobs/details", job="https://jobs.lever.co/palantir/dab396d4-2f14-4796-aac0-0d82883dccf0")
    job = body["job"]
    assert job["title"] and job["company"]["name"] == "Palantir Technologies" and job["company"]["logo"]
    assert job["description"]["full_text"] and job["description"]["body"]["html"]
    assert call("/lever/jobs/details", job=job["id"], company="palantir")["job"]["id"] == job["id"]
    assert call("/lever/jobs/details", job=f"palantir/{job['id']}")["job"]["title"] == job["title"]


def test_job_application_form():
    form = call("/lever/jobs/application-form", job="https://jobs.lever.co/palantir/dab396d4-2f14-4796-aac0-0d82883dccf0")
    assert form["job"]["company"]["slug"] == "palantir" and form["job"]["title"]
    assert form["fields"][0]["name"] == "resume" and form["is_resume_required"] in (True, False)
    assert form["question_count"] >= len(form["fields"]) and form["has_captcha"]


def test_jobs_search():
    wide = call("/lever/jobs/search", query="software engineer", workplace_type="remote", per_page=5)
    assert wide["index"]["companies_indexed"] > 0 and len(wide["jobs"]) <= 5
    assert all(j["workplace_type"] == "remote" for j in wide["jobs"])
    targeted = call("/lever/jobs/search", companies="spotify,palantir", query="engineer", sort="company")
    assert targeted["count"] > 10 and "index" not in targeted
    assert {j["company"]["slug"] for j in targeted["jobs"]} <= {"spotify", "palantir"}
    assert call("/lever/jobs/search", location="London", posted_since="90d", per_page=3)["jobs"]


def test_jobs_new():
    feed = call("/lever/jobs/new", since="7d", per_page=5)
    assert feed["since"].endswith("Z") and feed["jobs"]
    assert feed["jobs"][0]["posted_at"] >= feed["jobs"][-1]["posted_at"] >= feed["since"]
    assert call("/lever/jobs/new", companies="spotify", since="2026-01-01")["count"] > 0


def test_errors():
    # paths via variables so the listing tooling never reads an error case
    # as a route's working example
    details, jobs, job = "/lever/companies/details", "/lever/companies/jobs", "/lever/jobs/details"
    form, resolve = "/lever/jobs/application-form", "/lever/companies/resolve"
    call(details, status=404, company="anthropic")
    call(details, status=400, company="https://boards.greenhouse.io/spotify")
    call(jobs, status=400, company="spotify", workplace_type="office")
    call(jobs, status=400, company="spotify", posted_since="yesterday")
    call(job, status=404, job="spotify/00000000-0000-0000-0000-000000000000")
    call(job, status=400, job="00000000-0000-0000-0000-000000000000")
    call(form, status=404, job="spotify/00000000-0000-0000-0000-000000000000")
    call(resolve, status=400, company="")
