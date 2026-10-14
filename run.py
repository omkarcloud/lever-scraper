"""Start the Lever Scraper API.

    python run.py            # http://localhost:8000
    PORT=9000 python run.py  # another port

Then:  curl "http://localhost:8000/companies/jobs?company=spotify"

Cross-company search (/jobs/search, /jobs/new, /companies/search) reads every
company in lever/directory.json into memory in the background at start-up
(~6 minutes); set LEVER_INDEX_WARM_AT_START=0 to skip that.
"""
import bottle
from cheroot import wsgi

import config
import routes  # noqa: F401  (mounts the routes on bottle's default app)
from lever.index import start_refresher


def main():
    app = bottle.default_app()
    start_refresher()
    print(f"Lever Scraper listening on http://localhost:{config.PORT}/")
    print(f"Try:  curl \"http://localhost:{config.PORT}/companies/jobs?company=spotify\"")
    server = wsgi.Server(("0.0.0.0", config.PORT), app, server_name="lever-scraper", numthreads=16)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
