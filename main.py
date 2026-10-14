"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json.
"""
import json
import os

from lever.companies import details as company_details
from lever.companies import jobs as company_jobs
from lever.jobs import details as job_details
from lever.refs import resolve_job

os.makedirs("output", exist_ok=True)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # a company's hiring profile: logo, website, open roles, jobs by team and location
    save("company_spotify.json", company_details("spotify"))

    # every open job at one company, with filters (20 per page)
    save("jobs_spotify_engineer.json", company_jobs("spotify", query="engineer", workplace_type="hybrid"))

    # one full posting — a link, company/id, or a bare id with company
    save("job_palantir_fdse.json", job_details(resolve_job(
        "https://jobs.lever.co/palantir/dab396d4-2f14-4796-aac0-0d82883dccf0")))
