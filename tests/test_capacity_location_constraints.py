"""Location constraints must survive the HTTP boundary, and quota must stay honest.

Two defects motivated these tests.

1. ``POST /api/capacity-search`` built its own request from a handful of scalars
   and never forwarded ``allowed_regions`` / ``allow_region_change``. An operator
   restricted to ``europe-west4`` received ``us-central1`` candidates, presented
   as compatible, with nothing in the answer saying the constraint had been
   dropped.
2. ``QUOTA_UNKNOWN`` must not promote a candidate past ``catalog_proposed``.
   "We could not read the quota" is not "the quota is fine".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from compute_agent.app import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def offline_capacity(monkeypatch):
    """Keep the test offline and deterministic.

    The advisor is exercised for real; only the two outbound network calls are
    stubbed, so region filtering, stage gating and candidate construction are the
    genuine production code paths.
    """
    from agentic_compute import capacity_advisor

    def fake_advice(*args, **kwargs):
        return {
            "status": "ok",
            "is_simulated": False,
            "recommended_zone": "zone-a",
            "recommendations": [],
        }

    monkeypatch.setattr(capacity_advisor, "query_capacity_advice", fake_advice)


def _quota(monkeypatch, status: str, is_known: bool, is_exceeded: bool):
    from agentic_compute import capacity_advisor

    def fake_quota(*args, **kwargs):
        return {
            "status": status,
            "is_known": is_known,
            "is_exceeded": is_exceeded,
            "quota_limit": 100 if is_known else None,
            "quota_usage": 10 if is_known else None,
            "limit": 100 if is_known else None,
            "usage": 10 if is_known else None,
            "reason": "stubbed",
        }

    monkeypatch.setattr(capacity_advisor, "check_quota_availability", fake_quota)


def test_an_allowed_region_is_the_region_actually_searched(client, monkeypatch):
    _quota(monkeypatch, "QUOTA_AVAILABLE", True, False)
    resp = client.post(
        "/api/capacity-search",
        json={"cpu_requested": 4, "allowed_regions": ["europe-west4"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["searched_region"] == "europe-west4"
    assert body["allowed_regions"] == ["europe-west4"]
    assert body["candidates"], "a permitted region must still produce candidates"
    for candidate in body["candidates"]:
        assert candidate["region"] == "europe-west4", (
            "a candidate outside the allowed region would be an unusable proposal"
        )


def test_a_region_outside_the_allow_list_yields_no_candidate_and_says_why(client, monkeypatch):
    _quota(monkeypatch, "QUOTA_AVAILABLE", True, False)
    resp = client.post(
        "/api/capacity-search",
        json={
            "cpu_requested": 4,
            "allowed_regions": ["europe-west4"],
            "target_region": "us-central1",
            "allow_region_change": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == []
    assert body["location_note"], "an empty result must explain itself"
    assert "europe-west4" in body["location_note"]
    assert "allow_region_change" in body["location_note"]


def test_allowing_a_region_change_lets_the_search_leave_the_allow_list(client, monkeypatch):
    _quota(monkeypatch, "QUOTA_AVAILABLE", True, False)
    resp = client.post(
        "/api/capacity-search",
        json={
            "cpu_requested": 4,
            "allowed_regions": ["europe-west4"],
            "target_region": "us-central1",
            "allow_region_change": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allow_region_change"] is True
    assert body["candidates"], "the operator explicitly permitted the change"
    assert all(c["region"] == "us-central1" for c in body["candidates"])


def test_a_multi_region_allow_list_admits_it_only_searched_one(client, monkeypatch):
    _quota(monkeypatch, "QUOTA_AVAILABLE", True, False)
    resp = client.post(
        "/api/capacity-search",
        json={"cpu_requested": 4, "allowed_regions": ["europe-west4", "europe-west1"]},
    )
    body = resp.json()
    assert body["searched_region"] == "europe-west4"
    assert "europe-west1" in body["location_note"]
    assert "not explored" in body["location_note"]


def test_unknown_quota_leaves_candidates_at_catalog_proposed(client, monkeypatch):
    _quota(monkeypatch, "QUOTA_UNKNOWN", False, False)
    resp = client.post("/api/capacity-search", json={"cpu_requested": 4})
    body = resp.json()
    assert body["candidates"]
    for candidate in body["candidates"]:
        assert candidate["quota_status"] == "QUOTA_UNKNOWN"
        assert candidate["state_stage"] == "catalog_proposed", (
            "an unreadable quota must not be presented as an authorised one"
        )


def test_exceeded_quota_also_stays_at_catalog_proposed(client, monkeypatch):
    _quota(monkeypatch, "QUOTA_EXCEEDED", True, True)
    resp = client.post("/api/capacity-search", json={"cpu_requested": 4})
    body = resp.json()
    assert body["candidates"]
    for candidate in body["candidates"]:
        assert candidate["state_stage"] == "catalog_proposed"


def test_known_and_sufficient_quota_authorises_the_next_stage(client, monkeypatch):
    _quota(monkeypatch, "QUOTA_AVAILABLE", True, False)
    resp = client.post("/api/capacity-search", json={"cpu_requested": 4})
    body = resp.json()
    assert body["candidates"]
    assert any(c["state_stage"] != "catalog_proposed" for c in body["candidates"]), (
        "with a verified quota at least one candidate must advance past the catalog stage"
    )
