"""Lever transport egress (2026-11-11): calls leave from the server's own IP;
after a block the sessions step to the gateway exit
(config.LEVER_FALLBACK_EXIT), and only when that is refused too — or for the
last retry of a call the gateway keeps failing — to the residential exit
(config.LEVER_FALLBACK_PROXY_COUNTRY). A company's own website
(site_html) never rides the gateway, and a board over the gateway's 10 MB cap
leaves it at once. Offline, no network.

    python -m pytest lever/test_fetch.py -q
"""
import json
import os
import sys

from urllib.parse import urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from lever import fetch as F  # noqa: E402
from scraper_utils.testing import hosted_only

RESI = "http://resi-us:1"
API = "https://api.lever.co/v0/postings/acme"
SITE = "https://www.acme.example/careers"
HOSTS = []            # (exit, host) of every request sent


class Resp:
    def __init__(self, status, text):
        self.status_code, self.text, self.headers = status, text, {}

    def json(self):
        return json.loads(self.text)


class Session:
    """What egress.new_session() hands out, answering per exit from `plan`
    (exit -> a status, an exception to raise, or a list of those)."""

    def __init__(self, exit, plan, seen):
        self.exit, self.plan, self.seen, self.hosts = exit, plan, seen, HOSTS

    def _answer(self, url, ok):
        self.seen.append(self.exit)
        self.hosts.append((self.exit, urlsplit(url).hostname))
        answer = self.plan.get(self.exit, 200)
        answer = answer.pop(0) if isinstance(answer, list) else answer
        if isinstance(answer, Exception):
            raise answer
        return Resp(answer, ok if answer == 200 else "<html>refused</html>")

    def get(self, url, **kwargs):
        if urlsplit(url).hostname.endswith("lever.co"):
            return self._answer(url, "[]" if "api." in url else "<html>board</html>")
        resp = self._answer(url, "<html>careers</html>")
        resp.headers = {"content-type": "text/html"}
        return resp

    def close(self):
        pass


@pytest.fixture
def ladder(monkeypatch):
    """(plan, seen): the exits' answers and the exit of every request sent."""
    plan, seen = {}, []
    del HOSTS[:]
    F._drop_session()
    monkeypatch.setattr(F, "_fallback_until", 0.0)
    monkeypatch.setattr(F, "_fallback_step", 1)
    monkeypatch.setattr(F.egress, "new_session", lambda exit=None, impersonate=None: Session(exit, plan, seen))
    monkeypatch.setattr(config, "residential_proxy", lambda c, states=None: f"http://resi-{c}:1")
    monkeypatch.setattr(config, "MAX_RETRIES", 3)
    monkeypatch.setattr(config, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(config, "LEVER_PROXY_COUNTRY", None)
    monkeypatch.setattr(config, "LEVER_FALLBACK_EXIT", "gateway")
    monkeypatch.setattr(config, "LEVER_FALLBACK_PROXY_COUNTRY", "us")
    yield plan, seen
    F._drop_session()


OK = []


def call():
    return F.get_json(API, {"mode": "json"})


@hosted_only
def test_default_is_own_ip_then_the_gateway_then_residential():
    assert config.LEVER_PROXY_COUNTRY is None
    assert config.LEVER_FALLBACK_EXIT == "gateway"
    assert config.LEVER_FALLBACK_PROXY_COUNTRY == "us"


@hosted_only
def test_fallback_steps(ladder, monkeypatch):
    assert config.lever_proxy() is None
    assert config.lever_fallback_exits() == ["gateway", "us"]
    assert [config.lever_fallback_proxy(step) for step in (1, 2, 3)] == ["gateway", RESI, RESI]
    monkeypatch.setattr(config, "LEVER_FALLBACK_EXIT", None)           # the ladder before the gateway
    assert config.lever_fallback_exits() == ["us"] and config.lever_fallback_proxy() == RESI
    monkeypatch.setattr(config, "LEVER_FALLBACK_PROXY_COUNTRY", None)
    assert config.lever_fallback_exits() == [] and config.lever_fallback_proxy() is None
    monkeypatch.setattr(config, "LEVER_PROXY_COUNTRY", "gateway")
    assert config.lever_proxy() == "gateway"
    monkeypatch.setattr(config, "LEVER_PROXY_COUNTRY", "gb")
    assert config.lever_proxy() == "http://resi-gb:1"


@hosted_only
def test_healthy_calls_stay_on_the_own_ip(ladder):
    plan, seen = ladder
    assert call() == OK and F.get_page("https://jobs.lever.co/acme") == "<html>board</html>"
    assert seen == [None, None]


@hosted_only
def test_a_refused_own_ip_costs_gateway_requests_not_residential(ladder, monkeypatch):
    plan, seen = ladder
    plan[None] = 403
    assert call() == OK
    assert seen == [None, "gateway"]
    assert call() == OK and call() == OK                                     # the cooldown: the gateway
    assert seen == [None, "gateway", "gateway", "gateway"] and F._fallback_step == 1
    monkeypatch.setattr(F, "_fallback_until", 0.0)                      # cooldown over: the own IP again
    plan[None] = 200
    call()
    assert seen[-1] is None


@hosted_only
def test_a_refused_gateway_steps_on_to_residential_and_stays(ladder):
    plan, seen = ladder
    plan.update({None: 429, "gateway": 403})
    assert call() == OK
    assert seen == [None, "gateway", RESI]
    call()
    assert seen == [None, "gateway", RESI, RESI] and F._fallback_step == 2
    plan[RESI] = [403, 200]                                             # refused on the last step: a fresh exit of that step
    call()
    assert seen[-2:] == [RESI, RESI] and F._fallback_step == 2


@hosted_only
def test_last_retry_of_a_call_the_gateway_keeps_failing_rides_residential(ladder):
    plan, seen = ladder
    plan[None] = 403
    call()                                                      # the sessions are on the gateway step now
    del seen[:]
    plan["gateway"] = [RuntimeError("GatewayError: gateway 504"), 502, 200]
    assert call() == OK
    assert seen == ["gateway", "gateway", RESI]
    assert call() == OK                                     # for that call only: no cooldown on the paid exit
    assert seen == ["gateway", "gateway", RESI, "gateway"] and F._fallback_step == 1


@hosted_only
def test_without_the_gateway_step_a_refusal_goes_straight_to_residential(ladder, monkeypatch):
    plan, seen = ladder
    monkeypatch.setattr(config, "LEVER_FALLBACK_EXIT", None)
    plan.update({None: 403, RESI: [RuntimeError("reset"), 200]})
    assert call() == OK
    assert seen == [None, RESI, RESI]                                   # the ladder it always had
    monkeypatch.setattr(config, "LEVER_FALLBACK_PROXY_COUNTRY", None)
    monkeypatch.setattr(F, "_fallback_until", 0.0)
    F._drop_session()
    del seen[:]
    plan[None] = [403, 200]
    assert call() == OK                                     # no fallback at all: just a rebuilt session
    assert seen == [None, None] and F._step() == 0


@hosted_only
def test_a_pinned_exit_still_carries_every_call(ladder, monkeypatch):
    plan, seen = ladder
    monkeypatch.setattr(config, "LEVER_PROXY_COUNTRY", "gateway")           # how the gateway was measured
    plan["gateway"] = [403, 200]
    assert call() == OK and seen == ["gateway", "gateway"]
    F._drop_session()
    del seen[:]
    monkeypatch.setattr(config, "LEVER_PROXY_COUNTRY", "gb")
    plan["http://resi-gb:1"] = [403, 200]
    assert call() == OK and seen == ["http://resi-gb:1", "http://resi-gb:1"]


@hosted_only
def test_a_company_website_never_rides_the_gateway(ladder, monkeypatch):
    plan, seen = ladder
    assert F.site_html(SITE) == "<html>careers</html>" and HOSTS == [(None, "www.acme.example")]   # the own IP
    plan[None] = 403
    call()                                                              # the sessions are on the gateway step now
    del HOSTS[:]
    assert call() == OK and F.site_html(SITE) == "<html>careers</html>" and call() == OK
    assert HOSTS == [("gateway", "api.lever.co"), (RESI, "www.acme.example"), ("gateway", "api.lever.co")]  # the exit behind it, as before
    monkeypatch.setattr(config, "LEVER_FALLBACK_PROXY_COUNTRY", None)   # nothing behind the gateway: the own IP
    F._drop_session()
    del HOSTS[:]
    plan[None] = 200
    assert call() == OK and F.site_html(SITE) == "<html>careers</html>"
    assert HOSTS == [("gateway", "api.lever.co"), (None, "www.acme.example")]
    monkeypatch.setattr(config, "LEVER_PROXY_COUNTRY", "gateway")           # pinned to the gateway: still not the website
    monkeypatch.setattr(F, "_fallback_until", 0.0)
    F._drop_session()
    del HOSTS[:]
    assert call() == OK and F.site_html(SITE) == "<html>careers</html>"
    assert HOSTS == [("gateway", "api.lever.co"), (None, "www.acme.example")]


@hosted_only
def test_a_board_over_the_gateway_limit_leaves_the_gateway_at_once(ladder):
    plan, seen = ladder
    plan[None] = 403
    call()                                                              # the sessions are on the gateway step now
    del seen[:]
    GatewayError = type("GatewayError", (Exception,), {})
    plan["gateway"] = [GatewayError("gateway 500 InternalServerErrorException for " + API), 200]
    assert call() == OK
    assert seen == ["gateway", RESI]                                        # one gateway request lost, not two
    assert call() == OK and seen == ["gateway", RESI, "gateway"] and F._fallback_step == 1
