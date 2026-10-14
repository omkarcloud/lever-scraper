"""Local filtering / sorting / paging of normalized jobs.

Lever's API filters (team, department, location, commitment) are exact and
case-sensitive and there is no keyword search at all, so every /lever/*
listing filters the board's rows here instead: case-insensitive substring
matches on the text facets, exact matches on the normalized enums
(workplace_type, country), a date floor on posted_at and a has_salary
switch. `query` matches when every word appears in the title (a phrase in
double quotes must appear whole)."""
import re

from lever import parsers as P

SORTS = ("newest", "oldest", "title", "company")
_WORD_RE = re.compile(r'"([^"]+)"|(\S+)')


def _terms(query):
    out = []
    for phrase, word in _WORD_RE.findall(query or ""):
        term = (phrase or word).strip().lower()
        if term:
            out.append(term)
    return out


def _contains(value, needle):
    return bool(value) and needle in value.lower()


def _any_contains(values, needle):
    return any(_contains(v, needle) for v in values or [])


def apply(rows, query=None, team=None, department=None, location=None, commitment=None,
          workplace_type=None, country=None, posted_since=None, has_salary=None):
    terms = _terms(query)
    floor = P.parse_iso(posted_since) if posted_since else None
    out = []
    for row in rows:
        title = (row.get("title") or "").lower()
        if terms and not all(t in title for t in terms):
            continue
        if team and not _contains(row.get("team"), team.lower()):
            continue
        if department and not _contains(row.get("department"), department.lower()):
            continue
        if location and not (_contains(row.get("location"), location.lower())
                             or _any_contains(row.get("all_locations"), location.lower())):
            continue
        if commitment and not _contains(row.get("commitment"), commitment.lower()):
            continue
        if workplace_type and row.get("workplace_type") != workplace_type:
            continue
        if country and row.get("country") != country:
            continue
        if floor:
            posted = P.parse_iso(row.get("posted_at"))
            if not posted or posted < floor:
                continue
        if has_salary is True and not row.get("salary"):
            continue
        if has_salary is False and row.get("salary"):
            continue
        out.append(row)
    return out


def sort(rows, key="newest"):
    if key == "oldest":
        return sorted(rows, key=lambda r: (r.get("posted_at") or "9999", r.get("title") or ""))
    if key == "title":
        return sorted(rows, key=lambda r: ((r.get("title") or "").lower(), r.get("posted_at") or ""))
    if key == "company":
        return sorted(rows, key=lambda r: (((r.get("company") or {}).get("name") or (r.get("company") or {}).get("slug") or "").lower(),
                                           r.get("posted_at") or ""))
    return sorted(rows, key=lambda r: (r.get("posted_at") or "", r.get("title") or ""), reverse=True)


def page(rows, page_number, per_page):
    start = (page_number - 1) * per_page
    return rows[start:start + per_page], P.pagination(page_number, per_page, len(rows))


def echo(**values):
    """The filters a caller applied, for the response (None when unused)."""
    return {k: v for k, v in values.items()}
