"""Lever normalizers: the postings API's objects and the hosted pages'
HTML -> one clean snake_case shape per entity.

Output conventions (shared with the other scrapers here): `link` for page
URLs (always absolute), `*_count` for counters, `is_*`/`has_*` for
booleans, ISO-8601 UTC timestamps ("2026-06-23T09:29:45Z") and ISO dates,
numbers as numbers, money as amount + `currency`, null for missing.

One job shape everywhere (company listing, cross-company search, details):

  job  {id, title, link, apply_link, company{slug, name, link},
        department, team, location, all_locations, commitment, level,
        workplace_type, country, salary, summary, posted_at}
       + details / include_description: description{opening, body,
         sections, closing, full_text}

Deliberately dropped as noise or duplicates:
  * `description` / `descriptionPlain` — the opening + body concatenated;
    both parts are emitted separately and `full_text` re-joins everything;
  * `salaryDescription` (HTML) — its plain-text twin is kept;
  * the `categories.level` field is kept but it is deprecated upstream and
    empty on nearly every board;
  * on the hosted pages: the board's CSS / colour theme, tracking params in
    links, the JSON-LD `description` (same text as the API's), the AI-use
    disclosure paragraph in the board footer;
  * on the apply form: hCaptcha / LinkedIn-autofill plumbing, hidden
    tracking inputs (origin, referer, timezone, socialReferralKey,
    resumeStorageId), the consent copy (only whether consent is asked);
  * JSON-LD `jobLocation.address` fields other than the locality are null
    on every board checked, so only the locality list is kept (as the API's
    `all_locations`).
"""
import html as html_lib
import json
import re
from datetime import datetime, timedelta, timezone

from lever import refs

SUMMARY_CHARS = 300
WORKPLACE_TYPES = {"remote": "remote", "hybrid": "hybrid", "on-site": "on_site", "onsite": "on_site",
                   "on_site": "on_site"}

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(?:br\s*/?|/p|/div|/li|/h[1-6]|/tr)\s*>", re.I)
_LI_RE = re.compile(r"<\s*li[^>]*>", re.I)
_LI_ITEM_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.I | re.S)
_META_RE = re.compile(r'<meta\s+(?:property|name)="([^"]+)"\s+content="([^"]*)"', re.I)
_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_HEADER_LOGO_RE = re.compile(r'<a[^>]*class="main-header-logo"[^>]*href="([^"]*)"[^>]*>\s*<img[^>]*src="([^"]*)"', re.S)
_HEADER_LOGO_ALT_RE = re.compile(r'<a[^>]*href="([^"]*)"[^>]*class="main-header-logo"[^>]*>\s*<img[^>]*src="([^"]*)"', re.S)
_HEADER_TEXT_RE = re.compile(r'<div class="main-header-text">(.*?)</div>', re.S)
_FOOTER_HOME_RE = re.compile(r'<div class="main-footer-text">\s*<p>\s*<a href="([^"]*)"', re.S)
_QUESTION_RE = re.compile(r'<li class="application-question([^"]*)">(.*?)</li>', re.S)
_LABEL_RE = re.compile(r'<div class="application-label[^"]*">(.*?)</div>', re.S)
_INPUT_NAME_RE = re.compile(r'<(input|select|textarea)\b([^>]*)>', re.I)
_ATTR_RE = re.compile(r'([a-zA-Z-]+)="([^"]*)"')
_OPTION_RE = re.compile(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', re.S)
_TEMPLATE_RE = re.compile(r'<input[^>]*value="([^"]*)"[^>]*name="(cards|surveysResponses)\[([^\]]+)\]\[baseTemplate\]"', re.S)
_TEMPLATE_RE2 = re.compile(r'<input[^>]*name="(cards|surveysResponses)\[([^\]]+)\]\[baseTemplate\]"[^>]*value="([^"]*)"', re.S)
_LOCATION_SELECT_RE = re.compile(r'data-qa="(?:opportunity-location-select|candidate-location-select)"[^>]*>(.*?)</select>', re.S)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


# ---- value helpers ------------------------------------------------------------

def text(value):
    if not isinstance(value, str):
        return None
    value = " ".join(value.replace("\xa0", " ").split())
    return value or None


def to_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def to_float(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def clean_html(value):
    """Rich text -> plain text with paragraph / list breaks kept."""
    if not isinstance(value, str) or not value.strip():
        return None
    out = _LI_RE.sub("- ", value)
    out = _BREAK_RE.sub("\n", out)
    out = html_lib.unescape(_TAG_RE.sub("", out)).replace("\xa0", " ").replace("\r", "")
    lines = [" ".join(line.split()) for line in out.split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out or None


def plain(value):
    """The API's *Plain fields: already text, but with stray nbsp / CRs."""
    if not isinstance(value, str):
        return None
    out = value.replace("\xa0", " ").replace("\r", "")
    lines = [" ".join(line.split()) for line in out.split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out or None


def html_or_none(value):
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    # Lever sends "<div>&nbsp;</div>" style empties for blank blocks
    return stripped if clean_html(stripped) else None


def iso_from_ms(value):
    """Lever's createdAt (epoch milliseconds) -> '2026-06-23T09:29:45Z'."""
    number = to_float(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def iso_date(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
    return match.group(1) if match else None


def parse_iso(value):
    """'2026-06-23' | '2026-06-23T09:29:45Z' -> aware datetime (UTC), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def workplace_type(value):
    key = text(value)
    return WORKPLACE_TYPES.get(key.lower()) if key else None


def country_code(value):
    code = text(value)
    return code.upper() if code and len(code) == 2 and code.isalpha() else None


def pagination(page, per_page, total):
    total = to_int(total) or 0
    return {"page": page, "items_per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total else 0,
            "total_count": total}


# ---- postings -------------------------------------------------------------------

def _categories(raw):
    cats = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
    location = text(cats.get("location"))
    all_locations = []
    for item in cats.get("allLocations") or []:
        item = text(item)
        if item and item not in all_locations:
            all_locations.append(item)
    if location and location not in all_locations:
        all_locations.insert(0, location)
    return {
        "department": text(cats.get("department")),
        "team": text(cats.get("team")),
        "location": location,
        "all_locations": all_locations,
        "commitment": text(cats.get("commitment")),
        "level": text(cats.get("level")),
    }


def salary(raw):
    block = raw.get("salaryRange") if isinstance(raw.get("salaryRange"), dict) else None
    description = plain(raw.get("salaryDescriptionPlain")) or clean_html(raw.get("salaryDescription"))
    if not block and not description:
        return None
    block = block or {}
    currency = text(block.get("currency"))
    return {
        "min": to_float(block.get("min")),
        "max": to_float(block.get("max")),
        "currency": currency.upper() if currency else None,
        "interval": text(block.get("interval")),
        "description": description,
    }


def _section(item):
    if not isinstance(item, dict):
        return None
    content = item.get("content") if isinstance(item.get("content"), str) else ""
    items = [t for t in (clean_html(m.group(1)) for m in _LI_ITEM_RE.finditer(content)) if t]
    return {
        "title": text(item.get("text")),
        "text": clean_html(content),
        "items": items,
        "html": html_or_none(content),
    }


def description(raw):
    """The posting's long-form blocks: opening, body, the list sections
    (requirements, benefits, ...) and the closing paragraph, plus the whole
    thing re-joined as one plain text."""
    opening_text = plain(raw.get("openingPlain")) or clean_html(raw.get("opening"))
    body_text = plain(raw.get("descriptionBodyPlain")) or clean_html(raw.get("descriptionBody"))
    if opening_text is None and body_text is None:
        # older boards only fill the combined fields
        body_text = plain(raw.get("descriptionPlain")) or clean_html(raw.get("description"))
    sections = [s for s in (_section(i) for i in raw.get("lists") or []) if s]
    closing_text = plain(raw.get("additionalPlain")) or clean_html(raw.get("additional"))
    parts = [opening_text, body_text]
    for section in sections:
        parts.append(section["title"])
        parts.append(section["text"])
    parts.append(closing_text)
    full_text = "\n\n".join(p for p in parts if p) or None
    return {
        "opening": {"text": opening_text, "html": html_or_none(raw.get("opening"))},
        "body": {"text": body_text, "html": html_or_none(raw.get("descriptionBody") or raw.get("description"))},
        "sections": sections,
        "closing": {"text": closing_text, "html": html_or_none(raw.get("additional"))},
        "full_text": full_text,
    }


def summary(raw):
    opening = plain(raw.get("openingPlain")) or plain(raw.get("descriptionPlain")) \
        or clean_html(raw.get("opening")) or clean_html(raw.get("description"))
    if not opening:
        return None
    flat = " ".join(opening.split())
    if len(flat) <= SUMMARY_CHARS:
        return flat
    cut = flat[:SUMMARY_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def company_ref(slug, region="us", name=None, logo=None):
    return {"slug": slug, "name": name, "link": refs.board_link(slug, region)}


def job(raw, slug, region="us", company=None, include_description=False):
    """One postings-API object -> the job shape. `company` is a company_ref
    (name filled in by the caller when known)."""
    raw = raw if isinstance(raw, dict) else {}
    job_id = text(raw.get("id"))
    out = {
        "id": job_id,
        "title": text(raw.get("text")),
        "link": text(raw.get("hostedUrl")) or refs.job_link(slug, job_id, region),
        "apply_link": text(raw.get("applyUrl")) or refs.apply_link(slug, job_id, region),
        "company": dict(company) if company else company_ref(slug, region),
    }
    out.update(_categories(raw))
    out.update({
        "workplace_type": workplace_type(raw.get("workplaceType")),
        "country": country_code(raw.get("country")),
        "salary": salary(raw),
        "summary": summary(raw),
        "posted_at": iso_from_ms(raw.get("createdAt")),
    })
    if include_description:
        out["description"] = description(raw)
    return out


def jobs(rows, slug, region="us", company=None, include_description=False):
    return [job(r, slug, region, company, include_description) for r in rows if isinstance(r, dict) and r.get("id")]


# ---- aggregates ------------------------------------------------------------------

def _count_values(values):
    counts = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return [{"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))]


def facets(job_rows):
    """Distinct filter values with job counts, from normalized jobs — the
    same lists the hosted board's filter bar offers (Location type,
    Location, Team, Work type), plus departments and countries."""
    locations = []
    for row in job_rows:
        locations.extend(row.get("all_locations") or [])
    return {
        "teams": _count_values(r.get("team") for r in job_rows),
        "departments": _count_values(r.get("department") for r in job_rows),
        "locations": _count_values(locations),
        "commitments": _count_values(r.get("commitment") for r in job_rows),
        "workplace_types": _count_values(r.get("workplace_type") for r in job_rows),
        "countries": _count_values(r.get("country") for r in job_rows),
    }


def stats(job_rows, now=None):
    """Hiring signals for a board: open roles, how many were posted in the
    last 7 / 30 days, remote share, salary-disclosure share."""
    now = now or datetime.now(timezone.utc)
    week, month = now - timedelta(days=7), now - timedelta(days=30)
    posted = [parse_iso(r.get("posted_at")) for r in job_rows]
    posted = [p for p in posted if p]
    total = len(job_rows)
    remote = sum(1 for r in job_rows if r.get("workplace_type") == "remote")
    with_salary = sum(1 for r in job_rows if r.get("salary"))
    return {
        "open_jobs": total,
        "posted_last_7_days": sum(1 for p in posted if p >= week),
        "posted_last_30_days": sum(1 for p in posted if p >= month),
        "oldest_posted_at": min(posted).strftime("%Y-%m-%dT%H:%M:%SZ") if posted else None,
        "newest_posted_at": max(posted).strftime("%Y-%m-%dT%H:%M:%SZ") if posted else None,
        "remote_share": round(remote / total, 3) if total else None,
        "salary_disclosed_share": round(with_salary / total, 3) if total else None,
    }


def countries_of(postings):
    out = set()
    for raw in postings or []:
        if isinstance(raw, dict):
            code = country_code(raw.get("country"))
            if code:
                out.add(code)
    return sorted(out)


# ---- hosted board page ----------------------------------------------------------

def metas(page_html):
    return {name.lower(): html_lib.unescape(content) for name, content in _META_RE.findall(page_html or "")}


def company_from_board_html(page_html, slug, region="us"):
    """jobs.lever.co/<slug> -> {name, logo, website, description,
    social_image}: what the API never returns about the company."""
    page_html = page_html or ""
    meta = metas(page_html)
    name = text(meta.get("twitter:title"))
    if not name:
        og = text(meta.get("og:title")) or ""
        name = text(re.sub(r"\s+jobs$", "", og, flags=re.I)) or None
    match = _HEADER_LOGO_RE.search(page_html) or _HEADER_LOGO_ALT_RE.search(page_html)
    website, logo = (text(match.group(1)), text(match.group(2))) if match else (None, None)
    if not website:
        home = _FOOTER_HOME_RE.search(page_html)
        website = text(home.group(1)) if home else None
    if website and website.startswith(("https://jobs.lever.co", "https://jobs.eu.lever.co", "#")):
        website = None
    intro = _HEADER_TEXT_RE.search(page_html)
    return {
        "name": name,
        "logo": logo or text(meta.get("twitter:image")) or None,
        "website": website,
        "description": clean_html(intro.group(1)) if intro else None,
        "social_image": text(meta.get("og:image")),
    }


def jsonld(page_html):
    """The schema.org JobPosting block of a hosted posting page, or None."""
    for match in _JSONLD_RE.finditer(page_html or ""):
        try:
            doc = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("@type") == "JobPosting":
            return doc
    return None


def company_from_jsonld(doc):
    org = (doc or {}).get("hiringOrganization")
    org = org if isinstance(org, dict) else {}
    return {"name": text(org.get("name")), "logo": text(org.get("logo"))}


def page_title(page_html):
    match = _TITLE_RE.search(page_html or "")
    return text(html_lib.unescape(match.group(1))) if match else None


# ---- application form ---------------------------------------------------------------

_STANDARD_SECTIONS = {
    "resume": "resume", "name": "personal", "pronouns": "personal", "email": "personal",
    "phone": "personal", "org": "personal", "location": "personal",
}


def _field_type(tag, attrs):
    if tag == "select":
        return "dropdown"
    if tag == "textarea":
        return "textarea"
    kind = (attrs.get("type") or "text").lower()
    return {"file": "file", "email": "email", "tel": "phone", "checkbox": "multiple_select",
            "radio": "multiple_choice", "number": "number", "date": "date", "url": "link"}.get(kind, "text")


def _options(block):
    """<option> labels of a select, without the empty-valued placeholder."""
    out = []
    for value, label_text in _OPTION_RE.findall(block):
        label_text = clean_html(label_text)
        if value.strip() and label_text and label_text not in out:
            out.append(label_text)
    return out or None


def _template(value):
    try:
        doc = json.loads(html_lib.unescape(value))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _template_questions(doc):
    out = []
    for field in doc.get("fields") or []:
        if not isinstance(field, dict):
            continue
        options = [text(o.get("text")) for o in field.get("options") or [] if isinstance(o, dict)]
        kind = text(field.get("type")) or "text"
        out.append({
            "id": text(field.get("id")),
            "text": text(field.get("text")),
            "description": clean_html(field.get("description")) if field.get("description") else None,
            "type": kind.replace("-", "_"),
            "is_required": bool(field.get("required")),
            "options": [o for o in options if o] or None,
        })
    return out


def _templates(page_html):
    found = {}
    for value, kind, card_id in _TEMPLATE_RE.findall(page_html):
        found[(kind, card_id)] = value
    for kind, card_id, value in _TEMPLATE_RE2.findall(page_html):
        found.setdefault((kind, card_id), value)
    cards, surveys = [], []
    for (kind, card_id), value in found.items():
        doc = _template(value)
        if not doc:
            continue
        block = {
            "id": card_id,
            "title": text(doc.get("text")),
            "instructions": clean_html(doc.get("instructions")) if doc.get("instructions") else None,
            "questions": _template_questions(doc),
        }
        (cards if kind == "cards" else surveys).append(block)
    return cards, surveys


def application_form(page_html):
    """jobs.lever.co/<slug>/<id>/apply -> the questions a candidate is
    asked: the standard contact fields, the location picker, the
    company's custom question cards and its demographic (EEO) surveys."""
    page_html = page_html or ""
    fields = []
    seen = set()
    for classes, block in _QUESTION_RE.findall(page_html):
        if "custom-question" in classes:
            continue   # custom cards come from their baseTemplate JSON below
        label_match = _LABEL_RE.search(block)
        label = clean_html(label_match.group(1)) if label_match else None
        is_required = "✱" in block or 'required' in block.lower()
        for tag, attr_text in _INPUT_NAME_RE.findall(block):
            attrs = {k.lower(): v for k, v in _ATTR_RE.findall(attr_text)}
            name = attrs.get("name")
            if not name or name in seen or attrs.get("type", "").lower() == "hidden":
                continue
            if name.startswith(("cards[", "surveysResponses[", "consent[")):
                continue   # consent is reported as asks_marketing_consent
            seen.add(name)
            options = None
            if tag.lower() == "select":
                options = _options(block)
            clean_label = (label or "").replace("✱", "").strip() or None
            fields.append({
                "name": name,
                "label": clean_label,
                "type": _field_type(tag.lower(), attrs),
                "is_required": is_required,
                "section": "links" if name.startswith("urls[") else _STANDARD_SECTIONS.get(name, "other"),
                "options": options,
            })
    cards, surveys = _templates(page_html)
    loc = _LOCATION_SELECT_RE.search(page_html)
    location_options = _options(loc.group(1)) if loc else None
    resume = next((f for f in fields if f["name"] == "resume"), None)
    return {
        "fields": fields,
        "location_options": location_options,
        "custom_questions": cards,
        "demographic_surveys": surveys,
        "accepts_resume": resume is not None,
        "is_resume_required": bool(resume and resume["is_required"]),
        "asks_marketing_consent": 'name="consent[marketing]"' in page_html,
        "has_captcha": "h-captcha" in page_html,
        "question_count": len(fields) + sum(len(c["questions"]) for c in cards),
    }
