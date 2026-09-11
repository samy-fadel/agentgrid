"""Defect 1-E: the same quota page was re-fetched once per machine type.

``search_compatible_capacity`` calls ``check_quota_availability`` inside its
per-machine-type loop, and every one of those calls issued a fresh
``compute.regions.get`` HTTP request. The answer is identical for every machine
type in a region -- the response is a list of that region's quotas -- so a single
search made eight nearly simultaneous round trips for one document.

Searching every permitted region (defect 1-D) multiplies that by the number of
regions. Left uncached, one dashboard click became dozens of live API calls, was
slow enough to be unusable, and pushed the project towards the Compute Engine
read rate limit -- whose own failure mode, RATE_LIMIT_EXCEEDED, this product then
reports as a capacity blocker.

The cache is deliberately short-lived and explicitly clearable: a quota reading
is a measurement, and serving a stale one as current would be exactly the kind
of dishonesty the rest of this module avoids.
"""

import time

import pytest

from agentic_compute import capacity_advisor as ca


@pytest.fixture(autouse=True)
def _clear_cache():
    ca.reset_quota_cache()
    yield
    ca.reset_quota_cache()


class _Resp:
    status_code = 200

    def json(self):
        return {
            "quotas": [
                {"metric": "CPUS", "limit": 100.0, "usage": 10.0},
                {"metric": "PREEMPTIBLE_CPUS", "limit": 100.0, "usage": 0.0},
                {"metric": "GPUS_ALL_REGIONS", "limit": 8.0, "usage": 0.0},
            ]
        }


@pytest.fixture()
def counting_transport(monkeypatch):
    """Make the network countable without pretending the numbers are real."""
    calls: list[str] = []

    class _Creds:
        valid = True
        token = "fake-token"

        def refresh(self, request):  # pragma: no cover - never reached
            raise AssertionError("credentials are already valid")

    monkeypatch.setattr(ca, "HAVE_GOOGLE_AUTH", True)
    monkeypatch.setattr("google.auth.default", lambda scopes=None: (_Creds(), "proj"))

    def _get(url, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(ca.requests, "get", _get)
    return calls


def test_the_same_url_is_fetched_once(counting_transport):
    for _ in range(8):
        ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert len(counting_transport) == 1, (
        f"one document, one fetch; got {len(counting_transport)} round trips"
    )


def test_distinct_urls_are_fetched_separately(counting_transport):
    ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    ca._fetch_quotas("https://example.invalid/regions/europe-west1", "regions.get")
    ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert len(counting_transport) == 2
    assert counting_transport[0] != counting_transport[1]


def test_the_cached_value_is_the_real_answer_not_a_placeholder(counting_transport):
    first = ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    second = ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert first["status"] == "ok"
    assert second == first
    assert second["quotas"]["CPUS"]["limit"] == 100.0


def test_a_cached_entry_expires(counting_transport, monkeypatch):
    monkeypatch.setattr(ca, "QUOTA_CACHE_TTL_SECONDS", 0.05)
    ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    time.sleep(0.08)
    ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert len(counting_transport) == 2, "a stale quota reading must not be served forever"


def test_a_ttl_of_zero_disables_the_cache(counting_transport, monkeypatch):
    monkeypatch.setattr(ca, "QUOTA_CACHE_TTL_SECONDS", 0)
    for _ in range(3):
        ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert len(counting_transport) == 3


def test_reset_clears_the_cache(counting_transport):
    ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    ca.reset_quota_cache()
    ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert len(counting_transport) == 2


def test_a_failed_fetch_is_not_cached_as_a_success(monkeypatch):
    """An outage must not be frozen in for the whole TTL."""
    calls = []

    monkeypatch.setattr(ca, "HAVE_GOOGLE_AUTH", True)
    monkeypatch.setattr("google.auth.default", lambda scopes=None: (_ok_creds(), "proj"))

    class _Boom:
        status_code = 503

        def json(self):
            return {}

    state = {"fail": True}

    def _get(url, **kwargs):
        calls.append(url)
        return _Boom() if state["fail"] else _Resp()

    monkeypatch.setattr(ca.requests, "get", _get)

    bad = ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert bad["status"] == "unavailable"

    state["fail"] = False
    good = ca._fetch_quotas("https://example.invalid/regions/europe-west4", "regions.get")
    assert good["status"] == "ok", "the failure must not have been cached"
    assert len(calls) == 2


def _ok_creds():
    class _Creds:
        valid = True
        token = "fake-token"

        def refresh(self, request):  # pragma: no cover
            raise AssertionError("already valid")

    return _Creds()


def test_a_full_search_reads_each_region_once(counting_transport):
    """The end-to-end effect: one search, one quota document per region."""
    from agentic_compute.capacity_search import search_compatible_capacity

    cands = search_compatible_capacity(
        profile={
            "workload_id": "wl-cache",
            "cpu_requested": 8,
            "allowed_regions": ["europe-west4", "europe-west1"],
            "allow_spot": False,
        },
        demo_mode=True,
    )
    assert cands, "the search must still return candidates"
    regional = [u for u in counting_transport if "/regions/" in u]
    assert len(set(regional)) == len(regional), f"duplicate regional fetches: {regional}"
    assert len(regional) <= 2, (
        f"two regions must cost at most two regional reads, got {len(regional)}: {regional}"
    )
