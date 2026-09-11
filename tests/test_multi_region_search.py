"""Defect 1-D: the capacity search only ever looked at one permitted region.

``search_compatible_capacity`` reduced the operator's ``allowed_regions`` list
to ``allowed_regions[0]`` and searched that region alone. An operator who
declared "europe-west4, europe-west1 or us-central1 are all acceptable" was
answered about europe-west4 only. If europe-west4 was out of quota the product
reported no compatible capacity, while two regions the operator had explicitly
authorised were never queried.

The HTTP route papered over this with a ``location_note`` saying the other
regions "were not explored" -- honest, but capability 1 is to *find* compatible
capacity across the permitted locations, not to explain that it did not look.

Second half of the defect: when the requested region was outside the allow-list
and ``allow_region_change`` was false, the function returned a bare ``[]``. The
caller could not distinguish "your location constraints exclude everything"
from "nothing in the catalogue matches your hardware".
"""

import pytest

from agentic_compute import capacity_advisor
from agentic_compute.capacity_advisor import resolve_search_regions
from agentic_compute.capacity_search import search_compatible_capacity
from agentic_compute.models import WorkloadProfile


THREE = ["europe-west4", "europe-west1", "us-central1"]


def _regions(candidates):
    return sorted({c.region for c in candidates})


def test_every_permitted_region_is_searched():
    cands = search_compatible_capacity(
        profile={
            "workload_id": "wl-multi",
            "cpu_requested": 8,
            "allowed_regions": THREE,
            "allow_spot": False,
        },
        demo_mode=True,
    )
    assert _regions(cands) == sorted(THREE), (
        "the operator authorised three regions; all three must be answered for"
    )


def test_candidates_are_produced_for_each_region_not_just_labelled():
    """Each region must carry its own quota verdict, not a copy of the first."""
    cands = search_compatible_capacity(
        profile={
            "workload_id": "wl-multi",
            "cpu_requested": 8,
            "allowed_regions": ["europe-west4", "europe-west1"],
            "allow_spot": False,
        },
        demo_mode=True,
    )
    per_region = {}
    for c in cands:
        per_region.setdefault(c.region, []).append(c)
    assert set(per_region) == {"europe-west4", "europe-west1"}
    for region, items in per_region.items():
        assert items, region
        # A candidate must describe the region it claims to be in.
        assert all(c.region == region for c in items)


def test_an_explicit_target_region_still_narrows_the_search():
    """Asking about one region must not fan out to the whole allow-list."""
    cands = search_compatible_capacity(
        profile={
            "workload_id": "wl-one",
            "cpu_requested": 8,
            "allowed_regions": THREE,
            "allow_spot": False,
        },
        target_region="europe-west1",
        demo_mode=True,
    )
    assert _regions(cands) == ["europe-west1"]


def test_single_region_profile_is_unchanged():
    cands = search_compatible_capacity(
        profile={
            "workload_id": "wl-single",
            "cpu_requested": 8,
            "allowed_regions": ["europe-west4"],
            "allow_spot": False,
        },
        demo_mode=True,
    )
    assert _regions(cands) == ["europe-west4"]


# --- resolve_search_regions: the single source of truth -------------------


def _profile(**kw):
    kw.setdefault("workload_id", "wl")
    return WorkloadProfile(**kw)


def test_resolver_returns_all_permitted_regions():
    regions, note = resolve_search_regions(_profile(allowed_regions=THREE))
    assert regions == THREE
    assert note is None


def test_resolver_explains_an_excluded_target_instead_of_returning_nothing():
    regions, note = resolve_search_regions(
        _profile(allowed_regions=["europe-west4"], allow_region_change=False),
        region="us-central1",
    )
    assert regions == []
    assert note and "us-central1" in note and "europe-west4" in note, note
    assert "allow_region_change" in note, note


def test_resolver_allows_an_outside_target_when_region_change_is_permitted():
    regions, note = resolve_search_regions(
        _profile(allowed_regions=["europe-west4"], allow_region_change=True),
        region="us-central1",
    )
    assert regions == ["us-central1"]
    assert note and "allow_region_change" in note


def test_resolver_caps_the_number_of_regions_and_says_so():
    """One search must not silently fan out into an unbounded number of API calls."""
    many = [f"region-{i}" for i in range(12)]
    regions, note = resolve_search_regions(_profile(allowed_regions=many))
    assert len(regions) == capacity_advisor.MAX_SEARCH_REGIONS
    assert regions == many[: capacity_advisor.MAX_SEARCH_REGIONS]
    assert note and str(capacity_advisor.MAX_SEARCH_REGIONS) in note, note
    for skipped in many[capacity_advisor.MAX_SEARCH_REGIONS:]:
        assert skipped in note, f"{skipped} must be named as not searched: {note}"


def test_resolver_falls_back_when_no_region_is_declared(monkeypatch):
    monkeypatch.delenv("CLOUDSDK_COMPUTE_REGION", raising=False)
    profile = _profile()
    profile.allowed_regions = []
    regions, note = resolve_search_regions(profile)
    assert regions == ["us-central1"]


def test_the_excluded_target_case_returns_no_candidates():
    cands = search_compatible_capacity(
        profile={
            "workload_id": "wl-excluded",
            "cpu_requested": 8,
            "allowed_regions": ["europe-west4"],
            "allow_region_change": False,
        },
        target_region="us-central1",
        demo_mode=True,
    )
    assert cands == []


# --- the HTTP route must report what was actually searched ----------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    return TestClient(app)


def test_route_reports_every_region_it_searched(client):
    r = client.post(
        "/api/capacity-search",
        json={
            "workload_id": "wl-http-multi",
            "cpu_requested": 8,
            "allowed_regions": THREE,
            "allow_spot": False,
            "demo_mode": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["searched_regions"]) == sorted(THREE), body
    returned = sorted({c["region"] for c in body["candidates"]})
    assert returned == sorted(THREE), returned
    # The old note claimed the other regions "were not explored". It must not
    # survive now that they are.
    assert "not explored" not in (body.get("location_note") or "")


def test_route_note_and_candidates_cannot_disagree(client):
    """The note used to be recomputed independently of the search, which is how
    the two drifted apart. They must now come from the same resolver."""
    r = client.post(
        "/api/capacity-search",
        json={
            "workload_id": "wl-http-excl",
            "cpu_requested": 8,
            "allowed_regions": ["europe-west4"],
            "region": "us-central1",
            "allow_region_change": False,
            "demo_mode": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidates"] == []
    assert body["searched_regions"] == []
    assert body["location_note"] and "us-central1" in body["location_note"]
