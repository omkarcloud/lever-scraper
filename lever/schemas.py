"""Marshmallow request schemas for every /lever/* route. Generic fields
come from the shared schema_fields.py; this module adds the Lever
resolvers. Every schema's load() output is the kwargs dict its endpoint
function takes.

ONE param per input (tripadvisor QueryOrIdField convention, never a sibling
`url`/`id` pair):

    company    a board slug (spotify) OR any jobs.lever.co / api.lever.co link of it
    companies  comma-separated slugs and/or board links (max 20)
    job        a posting link, <company>/<posting id>, OR a bare posting id
               (then `company` names the board, or the id must be indexed)
    company    (companies/resolve) a company name, a website link / domain
               OR a jobs.lever.co link — whichever the caller has
"""
import re
from datetime import datetime, timedelta, timezone

from marshmallow import ValidationError, validate

from schema_fields import (BaseSchema, ChoiceField, CountryCodeField, Flag, PageField, PageSizeField,
                           PositiveInt, QueryField, RefField, StrippedString)
from lever import filters, refs

WORKPLACE_TYPES = ["remote", "hybrid", "on_site"]
_RELATIVE_RE = re.compile(r"^(\d{1,4})\s*(h|hours?|d|days?|w|weeks?|m|months?)$", re.I)
COMPANY_SORTS = ["open_jobs", "name"]
DEFAULT_NEW_DAYS = 7


class CompanyField(RefField):
    resolver = staticmethod(refs.resolve_company)


class CompanyListField(RefField):
    resolver = staticmethod(refs.resolve_companies)


class JobField(RefField):
    resolver = staticmethod(refs.resolve_job)


class LookupField(RefField):
    resolver = staticmethod(refs.resolve_lookup)


class SinceField(StrippedString):
    """A moment: relative ('24h', '7d', '2w', '3m') or absolute (YYYY-MM-DD or
    an ISO datetime) -> an ISO UTC timestamp. Relative values are rounded
    down to the hour so the same '7d' shares a cache row for an hour (the
    greenhouse convention). default=N days when the caller omits it."""

    def __init__(self, default=None, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", (lambda: self_default(default)) if default else None)
        super().__init__(**kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        if value is None:
            return None
        match = _RELATIVE_RE.match(value)
        if match:
            amount, unit = int(match.group(1)), match.group(2).lower()[0]
            delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount),
                     "m": timedelta(days=30 * amount)}[unit]
            if delta > timedelta(days=3660):
                raise ValidationError("At most 10 years back.")
            return (_hour_now() - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError("Must be relative (24h, 7d, 2w, 3m) or a date (YYYY-MM-DD or ISO datetime).")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed > datetime.now(timezone.utc) + timedelta(days=1):
            raise ValidationError("Must not be in the future.")
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hour_now():
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def self_default(relative):
    """'7d' -> the ISO timestamp it means right now (hour-rounded)."""
    return SinceField()._deserialize(relative, None, None)


def _max_length(n):
    """Length(max=n) that tolerates the None an optional StrippedString
    yields for an empty value (`?team=`), which the plain validator would
    len() into a TypeError -> bottle's HTML 500 instead of a no-op."""
    return lambda value: value is None or bool(validate.Length(max=n)(value))


def _text_filter():
    return StrippedString(required=False, load_default=None, validate=_max_length(100))


class JobFiltersSchema(BaseSchema):
    """The filters every job listing takes (companies/jobs, jobs/search,
    jobs/new)."""
    query = QueryField(required=False, load_default=None, validate=_max_length(200))
    team = _text_filter()
    department = _text_filter()
    location = _text_filter()
    commitment = _text_filter()
    workplace_type = ChoiceField(WORKPLACE_TYPES)
    country = CountryCodeField()
    posted_since = SinceField()
    has_salary = Flag()
    sort = ChoiceField(list(filters.SORTS), load_default="newest")
    page = PageField(max_page=1000)
    per_page = PageSizeField(default=20, max_size=100)


# ---- schemas ---------------------------------------------------------------------------

class CompanySchema(BaseSchema):
    company = CompanyField()


class CompanyJobsSchema(JobFiltersSchema):
    company = CompanyField()
    include_description = Flag(load_default=False)


class CompanySearchSchema(BaseSchema):
    query = QueryField(required=False, load_default=None, validate=_max_length(100))
    country = CountryCodeField()
    min_open_jobs = PositiveInt(max_value=100000)
    sort = ChoiceField(COMPANY_SORTS, load_default="open_jobs")
    page = PageField(max_page=1000)
    per_page = PageSizeField(default=20, max_size=100)


class ResolveSchema(BaseSchema):
    company = LookupField()


class JobSchema(BaseSchema):
    job = JobField()
    company = CompanyField(required=False, load_default=None)


class JobsSearchSchema(JobFiltersSchema):
    companies = CompanyListField(required=False, load_default=None)


class JobsNewSchema(JobFiltersSchema):
    since = SinceField(default=f"{DEFAULT_NEW_DAYS}d")
    companies = CompanyListField(required=False, load_default=None)
