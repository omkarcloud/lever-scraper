"""Offline tests: ref parsing, the request schemas, the local filters and
the normalizers against raw Lever responses captured 2026-10-14
(lever/fixtures/*): the Spotify board's postings-API rows (US shard), an
EU-shard board (aavelabs), a Zoox posting with a salary range, and the
Spotify board / posting / apply pages.

    python -m pytest lever/test_parsers.py -q
"""
import json
import os

import pytest

from schema_fields import load_query
from lever import filters
from lever import parsers as P
from lever import refs
from lever import schemas as S

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fx(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f) if name.endswith(".json") else f.read()


def _no_camel_or_url_keys(obj, path="$"):
    """Every emitted key is snake_case and no key is named *url*."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert key == key.lower() and " " not in key and "-" not in key, f"{path}.{key} not snake_case"
            assert "url" not in key, f"{path}.{key} should be a *link"
            _no_camel_or_url_keys(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _no_camel_or_url_keys(value, f"{path}[{i}]")


def _no_empty_strings(obj, path="$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            _no_empty_strings(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _no_empty_strings(value, f"{path}[{i}]")
    else:
        assert obj != "", f"{path} is an empty string (must be null)"


# ---- refs -----------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("spotify", "spotify"), (" Spotify ", "spotify"),
    ("https://jobs.lever.co/spotify", "spotify"),
    ("https://jobs.lever.co/Spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1/apply?lever-via=x", "spotify"),
    ("jobs.eu.lever.co/aavelabs", "aavelabs"),
    ("https://api.lever.co/v0/postings/spotify?mode=json", "spotify"),
    ("https://api.eu.lever.co/v0/postings/aavelabs/7017db1b-5cb8-440f-916f-f2e0fe5137a9", "aavelabs"),
])
def test_resolve_company(value, expected):
    assert refs.resolve_company(value) == expected


@pytest.mark.parametrize("value", ["", "spot ify", "a/b", "https://boards.greenhouse.io/spotify",
                                   "https://jobs.lever.co/", "https://api.lever.co/v1/postings/x", "apply"])
def test_resolve_company_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_company(value)


def test_resolve_companies():
    assert refs.resolve_companies("spotify, https://jobs.lever.co/palantir,SPOTIFY") == ["spotify", "palantir"]
    with pytest.raises(ValueError):
        refs.resolve_companies(",".join(f"c{n}" for n in range(21)))
    with pytest.raises(ValueError):
        refs.resolve_companies(" , ")


@pytest.mark.parametrize("value, expected", [
    ("https://jobs.lever.co/spotify/2193DB3F-77c5-43b8-b030-8f92c9882bf1",
     {"slug": "spotify", "id": "2193db3f-77c5-43b8-b030-8f92c9882bf1"}),
    ("https://jobs.eu.lever.co/aavelabs/7017db1b-5cb8-440f-916f-f2e0fe5137a9/apply",
     {"slug": "aavelabs", "id": "7017db1b-5cb8-440f-916f-f2e0fe5137a9"}),
    ("spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1", {"slug": "spotify", "id": "2193db3f-77c5-43b8-b030-8f92c9882bf1"}),
    ("2193db3f-77c5-43b8-b030-8f92c9882bf1", {"slug": None, "id": "2193db3f-77c5-43b8-b030-8f92c9882bf1"}),
])
def test_resolve_job(value, expected):
    assert refs.resolve_job(value) == expected


@pytest.mark.parametrize("value", ["", "spotify", "https://jobs.lever.co/spotify", "spotify/not-a-uuid",
                                   "https://example.com/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1"])
def test_resolve_job_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_job(value)


@pytest.mark.parametrize("value, expected", [
    ("https://jobs.lever.co/spotify", {"kind": "lever", "value": "spotify"}),
    ("https://www.spotify.com/jobs", {"kind": "site", "value": "https://www.spotify.com/jobs"}),
    ("spotify.com", {"kind": "site", "value": "https://spotify.com"}),
    ("Mistral  AI", {"kind": "name", "value": "Mistral AI"}),
])
def test_resolve_lookup(value, expected):
    assert refs.resolve_lookup(value) == expected


def test_slug_guesses():
    assert refs.slug_guesses("Alice & Bob")[:2] == ["aliceandbob", "alice-and-bob"]
    assert "alice-bob" in refs.slug_guesses("https://www.alice-bob.com/careers")
    assert refs.slug_guesses("acme.co.uk") == ["acme"]
    assert refs.slug_guesses("Mistral AI Inc.") == ["mistralai", "mistral-ai", "mistral_ai", "mistral"]
    assert refs.slug_guesses("") == []


def test_links():
    assert refs.board_link("spotify") == "https://jobs.lever.co/spotify"
    assert refs.board_link("aavelabs", "eu") == "https://jobs.eu.lever.co/aavelabs"
    assert refs.apply_link("spotify", "abc", "us") == "https://jobs.lever.co/spotify/abc/apply"
    assert refs.api_link("aavelabs", "eu") == "https://api.eu.lever.co/v0/postings/aavelabs?mode=json"


# ---- schemas --------------------------------------------------------------------------

def test_schemas_defaults_and_resolution():
    data, err = load_query(S.CompanyJobsSchema, {"company": "https://jobs.lever.co/spotify", "workplace_type": "On_Site",
                                                 "posted_since": "2026-10-01", "per_page": "5", "has_salary": "yes"})
    assert err is None
    assert data["company"] == "spotify" and data["workplace_type"] == "on_site"
    assert data["posted_since"] == "2026-10-01T00:00:00Z" and data["per_page"] == 5 and data["has_salary"] is True
    assert data["page"] == 1 and data["sort"] == "newest" and data["include_description"] is False

    data, err = load_query(S.JobsNewSchema, {})
    assert err is None and data["since"].endswith(":00:00Z") and data["companies"] is None

    data, err = load_query(S.JobsSearchSchema, {"companies": "spotify,palantir", "query": "engineer", "country": "se"})
    assert err is None and data["companies"] == ["spotify", "palantir"] and data["country"] == "SE"

    data, err = load_query(S.JobSchema, {"job": "2193db3f-77c5-43b8-b030-8f92c9882bf1", "company": "spotify"})
    assert err is None and data["job"]["slug"] is None and data["company"] == "spotify"

    data, err = load_query(S.ResolveSchema, {"company": "spotify.com"})
    assert err is None and data["company"]["kind"] == "site"


@pytest.mark.parametrize("schema, query, field", [
    (S.CompanySchema, {}, "company"),
    (S.CompanySchema, {"company": "https://boards.greenhouse.io/x"}, "company"),
    (S.CompanyJobsSchema, {"company": "spotify", "workplace_type": "office"}, "workplace_type"),
    (S.CompanyJobsSchema, {"company": "spotify", "posted_since": "yesterday"}, "posted_since"),
    (S.CompanyJobsSchema, {"company": "spotify", "per_page": "500"}, "per_page"),
    (S.CompanyJobsSchema, {"company": "spotify", "bogus": "1"}, "bogus"),
    (S.JobSchema, {"job": "spotify"}, "job"),
    (S.JobsNewSchema, {"since": "2099-01-01"}, "since"),
    (S.CompanySearchSchema, {"country": "XXX"}, "country"),
])
def test_schemas_reject(schema, query, field):
    data, err = load_query(schema, query)
    assert data is None and field in err["errors"]


def test_since_relative():
    data, _ = load_query(S.JobsNewSchema, {"since": "24h"})
    assert data["since"].endswith(":00:00Z")
    data, _ = load_query(S.CompanyJobsSchema, {"company": "spotify", "posted_since": "2w"})
    assert data["posted_since"].endswith(":00:00Z")


# ---- postings -------------------------------------------------------------------------

def test_job_shape():
    rows = fx("postings_spotify.json")
    company = P.company_ref("spotify", "us", "Spotify")
    jobs = P.jobs(rows, "spotify", "us", company)
    assert len(jobs) == 3
    first = jobs[0]
    _no_camel_or_url_keys(first)
    _no_empty_strings(first)
    assert list(first)[:5] == ["id", "title", "link", "apply_link", "company"]
    assert first["company"] == {"slug": "spotify", "name": "Spotify", "link": "https://jobs.lever.co/spotify"}
    assert first["link"] == "https://jobs.lever.co/spotify/" + first["id"]
    assert first["apply_link"].endswith("/apply")
    assert first["department"] == "Engineering" and first["team"] == "Experience"
    assert first["location"] == "London" and first["all_locations"] == ["London", "Stockholm"]
    assert first["commitment"] == "Permanent" and first["level"] is None
    assert first["workplace_type"] == "hybrid" and first["country"] == "GB"
    assert first["salary"] is None
    assert first["posted_at"] == "2026-06-23T11:29:45Z"
    assert first["summary"].startswith("We design Spotify") and first["summary"].endswith("…")
    assert len(first["summary"]) <= P.SUMMARY_CHARS + 1
    assert "description" not in first


def test_job_description():
    raw = fx("posting_spotify.json")
    job = P.job(raw, "spotify", "us", include_description=True)
    desc = job["description"]
    assert list(desc) == ["opening", "body", "sections", "closing", "full_text"]
    assert desc["body"]["text"].startswith("We design Spotify") and desc["body"]["html"].startswith("<")
    assert [s["title"] for s in desc["sections"]] == ["What You'll Do", "Who You Are", "Where You'll Be"]
    assert desc["sections"][0]["items"] and "<li" in desc["sections"][0]["html"]
    assert desc["closing"]["text"].startswith("Spotify is an equal opportunity employer")
    assert "What You'll Do" in desc["full_text"] and desc["full_text"].startswith(desc["body"]["text"][:50])
    _no_empty_strings(job)


def test_salary_and_eu_board():
    zoox = P.job(fx("posting_zoox_salary.json"), "zoox", "us")
    assert zoox["salary"]["min"] == 123000 and zoox["salary"]["max"] == 168000
    assert zoox["salary"]["currency"] == "USD" and zoox["salary"]["interval"] == "per-year-salary"
    assert zoox["salary"]["description"].startswith("Base Salary Range")
    eu = P.jobs(fx("postings_aavelabs_eu.json"), "aavelabs", "eu")
    assert eu[0]["link"].startswith("https://jobs.eu.lever.co/aavelabs/")
    assert eu[0]["workplace_type"] == "remote" and eu[0]["company"]["link"] == "https://jobs.eu.lever.co/aavelabs"


def test_job_never_crashes_on_partial():
    job = P.job({"id": "x"}, "acme", "us")
    assert job["title"] is None and job["all_locations"] == [] and job["salary"] is None
    assert job["link"] == "https://jobs.lever.co/acme/x" and job["summary"] is None
    assert P.job({"id": "x", "categories": None, "lists": "junk", "createdAt": "nope"}, "acme", "us",
                 include_description=True)["description"]["sections"] == []
    assert P.jobs([None, "x", {"text": "no id"}], "acme", "us") == []


def test_facets_and_stats():
    jobs = P.jobs(fx("postings_spotify.json"), "spotify", "us")
    facets = P.facets(jobs)
    assert {"name": "Engineering", "count": 1} in facets["departments"]
    assert [f["count"] for f in facets["departments"]] == sorted((f["count"] for f in facets["departments"]), reverse=True)
    assert {"name": "London", "count": 1} in facets["locations"] and {"name": "Stockholm", "count": 1} in facets["locations"]
    assert facets["workplace_types"] and {"name": "GB", "count": 1} in facets["countries"]
    stats = P.stats(jobs)
    assert stats["open_jobs"] == 3 and stats["newest_posted_at"] >= stats["oldest_posted_at"]
    assert 0 <= stats["remote_share"] <= 1 and stats["salary_disclosed_share"] == 0
    assert P.countries_of(fx("postings_spotify.json")) == sorted(P.countries_of(fx("postings_spotify.json")))


def test_workplace_and_helpers():
    assert P.workplace_type("on-site") == "on_site" and P.workplace_type("unspecified") is None
    assert P.iso_from_ms(1782214185805) == "2026-06-23T11:29:45Z" and P.iso_from_ms("x") is None
    assert P.parse_iso("2026-06-23").isoformat().startswith("2026-06-23T00:00:00+00:00")
    assert P.clean_html("<div>a&nbsp;b<br>c</div><ul><li>d</li></ul>") == "a b\nc\n- d"
    assert P.pagination(2, 20, 45) == {"page": 2, "items_per_page": 20, "total_pages": 3, "total_count": 45}


# ---- hosted pages ---------------------------------------------------------------------

def test_company_from_board_html():
    profile = P.company_from_board_html(fx("board_spotify.html"), "spotify")
    assert profile["name"] == "Spotify" and profile["website"] == "http://spotifyjobs.com"
    assert profile["logo"].startswith("https://lever-client-logos") and profile["social_image"].startswith("https://")
    assert profile["description"] is None
    assert P.company_from_board_html("", "x") == {"name": None, "logo": None, "website": None,
                                                  "description": None, "social_image": None}


def test_jsonld():
    doc = P.jsonld(fx("posting_spotify.html"))
    assert doc["@type"] == "JobPosting" and doc["datePosted"] == "2026-06-23"
    assert P.company_from_jsonld(doc) == {"name": "Spotify", "logo": doc["hiringOrganization"]["logo"]}
    assert P.jsonld("<html></html>") is None and P.company_from_jsonld(None) == {"name": None, "logo": None}


def test_application_form():
    form = P.application_form(fx("apply_spotify.html"))
    _no_camel_or_url_keys(form)
    _no_empty_strings(form)
    names = [f["name"] for f in form["fields"]]
    assert names[:6] == ["resume", "name", "pronouns", "email", "phone", "org"]
    assert "urls[LinkedIn]" in names and "consent[marketing]" not in names
    resume = form["fields"][0]
    assert resume["type"] == "file" and resume["is_required"] and resume["section"] == "resume"
    assert form["location_options"] == ["London", "Stockholm"]
    assert form["is_resume_required"] and form["accepts_resume"] and form["has_captcha"] and form["asks_marketing_consent"]
    cards = form["custom_questions"]
    assert [c["title"] for c in cards] == ["Previous Spotify Employment",
                                           "Role requires candidate to be based in London or Stockholm",
                                           "Years of Android Experience"]
    q = cards[0]["questions"][0]
    assert q["type"] == "multiple_select" and q["is_required"] and q["options"] == ["No", "Yes - Intern", "Yes - Full Time Employment"]
    assert cards[2]["questions"][0]["type"] == "multiple_choice" and not cards[2]["questions"][0]["is_required"]
    surveys = form["demographic_surveys"]
    assert len(surveys) == 4 and surveys[0]["instructions"].startswith("We’d like to know")
    assert surveys[0]["questions"][0]["text"] == "What best describes your gender?"
    assert form["question_count"] == len(form["fields"]) + 3
    assert P.application_form("")["fields"] == [] and P.application_form("")["question_count"] == 0


# ---- filters --------------------------------------------------------------------------

def _rows():
    return P.jobs(fx("postings_spotify.json"), "spotify", "us") + P.jobs(fx("postings_aavelabs_eu.json"), "aavelabs", "eu")


def test_filters():
    rows = _rows()
    assert [r["title"] for r in filters.apply(rows, query="android engineer")] == ["Android Engineer - Experience"]
    assert filters.apply(rows, query='"art director"') and not filters.apply(rows, query='"director art"')
    assert all(r["workplace_type"] == "remote" for r in filters.apply(rows, workplace_type="remote"))
    assert len(filters.apply(rows, location="stockholm")) == 1
    assert len(filters.apply(rows, country="GB")) == 1 and filters.apply(rows, country="ZZ") == []
    assert len(filters.apply(rows, posted_since="2026-06-23T00:00:00Z")) < len(rows)
    assert filters.apply(rows, has_salary=True) == [] and len(filters.apply(rows, has_salary=False)) == len(rows)
    assert filters.apply(rows, team="experience")[0]["team"] == "Experience"


def test_sort_and_page():
    rows = _rows()
    newest = filters.sort(rows, "newest")
    assert [r["posted_at"] for r in newest] == sorted((r["posted_at"] for r in rows), reverse=True)
    assert filters.sort(rows, "oldest")[0] == newest[-1]
    assert [r["title"] for r in filters.sort(rows, "title")] == sorted(r["title"] for r in rows)
    by_company = filters.sort(rows, "company")
    assert by_company[0]["company"]["slug"] == "aavelabs"
    page_rows, pagination = filters.page(rows, 2, 2)
    assert len(page_rows) == 2 and pagination == {"page": 2, "items_per_page": 2, "total_pages": 3, "total_count": 5}
