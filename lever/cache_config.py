"""Cache TTL per /lever/* endpoint (cache.py, keyed on the validated params).

A board changes when a recruiter publishes or closes a role — a few times
a day at the busiest companies, weekly at most. A posting's text almost
never changes after publication; its application form is set once.
"""
from datetime import timedelta

# --- companies ---------------------------------------------------------------
COMPANY_CACHE = timedelta(hours=1)          # companies/details (profile + facets + stats)
COMPANY_JOBS_CACHE = timedelta(minutes=30)  # companies/jobs (a listing)
COMPANY_SEARCH_CACHE = timedelta(hours=1)   # companies/search (the directory)
RESOLVE_CACHE = timedelta(days=1)           # companies/resolve (which slug a company uses)

# --- jobs --------------------------------------------------------------------
JOB_CACHE = timedelta(hours=6)              # jobs/details
APPLICATION_FORM_CACHE = timedelta(days=1)  # jobs/application-form
SEARCH_CACHE = timedelta(minutes=15)        # jobs/search, jobs/new (the index refreshes hourly)
