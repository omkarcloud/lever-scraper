"""Lever reference parsing: ONE param per input that auto-detects its forms
(tripadvisor QueryOrIdField convention — never a sibling `url`/`id` pair).

  company    spotify (the board slug — the path segment after jobs.lever.co/)
             | https://jobs.lever.co/spotify | https://jobs.eu.lever.co/aavelabs
             | https://api.lever.co/v0/postings/spotify?mode=json
             | jobs.lever.co/spotify/<posting id>  (the slug part is used)
  companies  comma-separated slugs and/or board links (max MAX_COMPANIES)
  job        https://jobs.lever.co/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1[/apply]
             | spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1
             | 2193db3f-77c5-43b8-b030-8f92c9882bf1  (a bare posting id; the
               endpoint then needs `company`, or the id must be in the index)
  lookup     anything that might identify a company for /lever/companies/resolve:
             a Lever link (-> slug), a website link / bare domain (-> the
             site is read for a Lever embed, then slug guesses are probed)
             or a company name (-> directory match, then slug guesses)

Slugs are case-insensitive on Lever (jobs.lever.co/Spotify and /spotify are
the same board), so they are lower-cased here and everywhere downstream.
"""
import re
from urllib.parse import urlparse

LEVER_HOSTS = {
    "jobs.lever.co": "us", "www.jobs.lever.co": "us", "api.lever.co": "us",
    "jobs.eu.lever.co": "eu", "api.eu.lever.co": "eu",
}
BOARD_HOST = {"us": "https://jobs.lever.co", "eu": "https://jobs.eu.lever.co"}
API_HOST = {"us": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# path segments on jobs.lever.co that are not boards
NOT_SLUGS = {"favicon.ico", "robots.txt", "static", "assets", "apply", "postings", "v0", "v1",
             "login", "logout", "sitemap.xml", "img", "images", "css", "js", "fonts", "api",
             "thanks", "embed", "confirm", "index.html"}
MAX_COMPANIES = 20
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$", re.I)


def _is_url(value):
    low = value.lower()
    return low.startswith(("http://", "https://", "//")) or any(
        low.startswith(h + "/") or low == h for h in LEVER_HOSTS)


def _parsed(value):
    url = value
    if url.startswith("//"):
        url = "https:" + url
    elif not url.lower().startswith("http"):
        url = "https://" + url
    return urlparse(url)


def _lever_segments(value):
    """A Lever link -> (region, [path segments]). ValueError for another site."""
    parsed = _parsed(value)
    host = (parsed.hostname or "").lower()
    region = LEVER_HOSTS.get(host)
    if region is None:
        raise ValueError("not a lever.co link")
    segments = [s for s in parsed.path.split("/") if s]
    if host.startswith("api."):
        # https://api.lever.co/v0/postings/<slug>[/<id>]
        if len(segments) >= 3 and segments[0] == "v0" and segments[1] == "postings":
            segments = segments[2:]
        else:
            raise ValueError("a Lever API link must look like https://api.lever.co/v0/postings/<company>")
    return region, segments


def _slug(value):
    slug = value.strip().lower()
    if not SLUG_RE.match(slug) or slug in NOT_SLUGS:
        raise ValueError(f"'{value}' is not a Lever board slug")
    return slug


# ---- links ------------------------------------------------------------------------

def board_link(slug, region="us"):
    return f"{BOARD_HOST.get(region) or BOARD_HOST['us']}/{slug}" if slug else None


def job_link(slug, job_id, region="us"):
    return f"{board_link(slug, region)}/{job_id}" if slug and job_id else None


def apply_link(slug, job_id, region="us"):
    link = job_link(slug, job_id, region)
    return f"{link}/apply" if link else None


def api_link(slug, region="us"):
    return f"{API_HOST.get(region) or API_HOST['us']}/{slug}?mode=json" if slug else None


# ---- resolvers ----------------------------------------------------------------------

def resolve_company(value):
    """Slug or any Lever link of the board -> the slug (lower-cased)."""
    value = str(value or "").strip()
    if not value:
        raise ValueError("company is required")
    if _is_url(value):
        try:
            _, segments = _lever_segments(value)
        except ValueError as e:
            raise ValueError(f"company must be a Lever board slug (spotify) or a jobs.lever.co link ({e})")
        if not segments:
            raise ValueError("company link must look like https://jobs.lever.co/spotify")
        return _slug(segments[0])
    if "/" in value or " " in value:
        raise ValueError("company must be a Lever board slug (spotify) or a jobs.lever.co link")
    return _slug(value)


def region_hint(value):
    """The region a Lever link names ('us' | 'eu'), None for a bare slug."""
    value = str(value or "").strip()
    if not _is_url(value):
        return None
    try:
        return _lever_segments(value)[0]
    except ValueError:
        return None


def resolve_companies(value):
    """Comma-separated slugs / links -> [slug, ...] (deduped, order kept)."""
    out = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        slug = resolve_company(raw)
        if slug not in out:
            out.append(slug)
    if not out:
        raise ValueError("companies is required")
    if len(out) > MAX_COMPANIES:
        raise ValueError(f"at most {MAX_COMPANIES} companies per call")
    return out


def resolve_job(value):
    """Posting link, 'slug/id' or a bare posting id -> {"slug": str | None,
    "id": str}."""
    value = str(value or "").strip()
    if not value:
        raise ValueError("job is required")
    if _is_url(value):
        try:
            _, segments = _lever_segments(value)
        except ValueError as e:
            raise ValueError(f"job must be a jobs.lever.co posting link or a posting id ({e})")
        if len(segments) < 2 or not JOB_ID_RE.match(segments[1]):
            raise ValueError("job link must look like "
                             "https://jobs.lever.co/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1")
        return {"slug": _slug(segments[0]), "id": segments[1].lower()}
    if "/" in value:
        slug, _, job_id = value.strip("/").partition("/")
        job_id = job_id.strip("/").split("/")[0]
        if not JOB_ID_RE.match(job_id):
            raise ValueError("job must be <company slug>/<posting id>, a posting link or a posting id")
        return {"slug": _slug(slug), "id": job_id.lower()}
    if not JOB_ID_RE.match(value):
        raise ValueError("job must be a Lever posting id (2193db3f-77c5-43b8-b030-8f92c9882bf1), "
                         "<company>/<posting id> or a jobs.lever.co posting link")
    return {"slug": None, "id": value.lower()}


def resolve_lookup(value):
    """A lookup for /lever/companies/resolve -> {"kind": "lever" | "site" |
    "name", "value": ...}: a Lever link gives the slug outright, a website
    link / domain is read for a Lever embed, a name is matched and guessed."""
    value = " ".join(str(value or "").split())
    if not value:
        raise ValueError("company is required")
    if _is_url(value):
        try:
            return {"kind": "lever", "value": resolve_company(value)}
        except ValueError:
            pass
        parsed = _parsed(value)
        host = (parsed.hostname or "").lower()
        if not host or "." not in host:
            raise ValueError("query must be a company name, a website link / domain or a jobs.lever.co link")
        return {"kind": "site", "value": parsed.geturl() if parsed.scheme else "https://" + value}
    if _DOMAIN_RE.match(value):
        return {"kind": "site", "value": "https://" + value.lower()}
    if len(value) > 100:
        raise ValueError("company must be under 100 characters")
    return {"kind": "name", "value": value}


def slug_guesses(name_or_host):
    """Candidate board slugs for a company name or hostname: 'Alice & Bob',
    'alice-bob.com' -> ['alicebob', 'alice-bob', 'alice_bob', 'alice']."""
    base = str(name_or_host or "").strip().lower()
    base = re.sub(r"^https?://", "", base).split("/")[0]
    if base.startswith("www."):
        base = base[4:]
    if _DOMAIN_RE.match(base):
        # the registrable label of a host: acme.com, acme.co.uk, jobs.acme.io -> acme
        labels = base.split(".")
        if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac", "gov"):
            base = labels[-3]
        else:
            base = labels[-2]
    base = re.sub(r"\b(inc|llc|ltd|gmbh|corp|corporation|company|co|limited|plc|ag|sa|srl)\b\.?", " ", base)
    base = re.sub(r"[&+]", " and ", base)
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    out = []
    for candidate in ("".join(words), "-".join(words), "_".join(words),
                      "".join(w for w in words if w != "and"), "-".join(w for w in words if w != "and"),
                      words[0] if len(words) > 1 else None):
        if candidate and SLUG_RE.match(candidate) and candidate not in out:
            out.append(candidate)
    return out[:6]
