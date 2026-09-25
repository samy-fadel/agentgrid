"""A slow route must not freeze the instance.

The live capacity search makes about 20 s of blocking Compute Engine calls.
Called directly inside an ``async def`` route it ran on the event loop, so
every other request of the instance waited behind it: in a real browser the
Cluster view's diagnosis only arrived once the search had finished, and the
5 s snapshot polling stalled with it.
"""

import asyncio
import time

import httpx
import pytest

import compute_agent.app as app_module
from agentic_compute import capacity_search

_SEARCH_SECONDS = 1.5


def _race_a_diagnosis_against_a_slow_search() -> tuple[float, float, int, int]:
    """Seconds until the diagnosis and the search complete, from one start."""

    async def scenario():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            t0 = time.monotonic()
            search = asyncio.create_task(client.get("/api/capacity-search?cpu_requested=2"))
            await asyncio.sleep(0.2)  # the search is now in flight
            diagnosis = await client.get("/api/diagnose?job_state=RUNNING")
            diagnosis_done = time.monotonic() - t0
            search_response = await search
            search_done = time.monotonic() - t0
            return diagnosis_done, search_done, diagnosis.status_code, search_response.status_code

    return asyncio.run(scenario())


@pytest.fixture()
def slow_search(monkeypatch):
    def search(**kwargs):
        time.sleep(_SEARCH_SECONDS)
        return []

    monkeypatch.setattr(capacity_search, "search_compatible_capacity", search)


def test_a_slow_capacity_search_does_not_freeze_the_other_routes(slow_search):
    diagnosis_done, search_done, diagnosis_status, search_status = (
        _race_a_diagnosis_against_a_slow_search()
    )

    assert (diagnosis_status, search_status) == (200, 200)
    assert search_done >= _SEARCH_SECONDS * 0.9, "the slow search did not run"
    assert diagnosis_done < _SEARCH_SECONDS * 0.6, (
        f"the diagnosis waited {diagnosis_done:.2f}s behind a {_SEARCH_SECONDS}s capacity search"
    )


def test_the_race_detects_a_route_that_blocks_the_loop(slow_search, monkeypatch):
    """Negative control: run the search inline again, and the race must see it."""

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(app_module, "run_in_threadpool", inline)

    diagnosis_done, _, _, _ = _race_a_diagnosis_against_a_slow_search()

    assert diagnosis_done >= _SEARCH_SECONDS * 0.9, (
        "a search blocking the event loop went unnoticed: the race proves nothing"
    )
