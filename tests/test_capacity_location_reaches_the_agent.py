"""Defect 1-F: the agent could not see why a capacity search came back empty.

``POST /api/capacity-search`` returns ``searched_regions``, ``allowed_regions``,
``allow_region_change`` and a ``location_note``. The two MCP surfaces -- the
``/search_capacity`` custom route and the ``search_capacity`` tool -- returned
``{"candidates": [...]}`` and nothing else.

So when the operator's location constraints excluded every region, the agent
received ``{"candidates": []}`` and had no way to tell "your constraints rule
this out" apart from "no machine in the catalogue fits your hardware". It would
report the wrong blocker to the operator, and the obvious next move -- relax the
region constraint -- would never be proposed.

Same family as defect 2-B: the library and the HTTP route knew something the
agent's only surface did not expose. This file also pins the tool signature
against the HTTP route so the two cannot drift apart again.
"""

import inspect

import pytest

from agentic_compute import mcp_server


THREE = ["europe-west4", "europe-west1", "us-central1"]


def _call_tool(**kwargs):
    return mcp_server.search_capacity(**kwargs)


def test_the_tool_reports_which_regions_it_searched():
    out = _call_tool(
        workload_id="wl-mcp-multi",
        cpu_requested=8,
        allowed_regions=THREE,
        allow_spot=False,
        demo_mode=True,
    )
    assert sorted(out["searched_regions"]) == sorted(THREE), out.keys()


def test_the_tool_searches_every_permitted_region():
    out = _call_tool(
        workload_id="wl-mcp-multi",
        cpu_requested=8,
        allowed_regions=["europe-west4", "europe-west1"],
        allow_spot=False,
        demo_mode=True,
    )
    assert sorted({c["region"] for c in out["candidates"]}) == [
        "europe-west1",
        "europe-west4",
    ]


def test_an_empty_result_is_explained_not_merely_empty():
    """The whole point: the agent must be able to name the real blocker."""
    out = _call_tool(
        workload_id="wl-mcp-excluded",
        cpu_requested=8,
        allowed_regions=["europe-west4"],
        target_region="us-central1",
        allow_region_change=False,
        demo_mode=True,
    )
    assert out["candidates"] == []
    assert out["searched_regions"] == []
    note = out["location_note"]
    assert note and "us-central1" in note and "europe-west4" in note, note
    assert "allow_region_change" in note, note


def test_the_tool_can_honour_an_explicit_region_change():
    out = _call_tool(
        workload_id="wl-mcp-change",
        cpu_requested=8,
        allowed_regions=["europe-west4"],
        target_region="us-central1",
        allow_region_change=True,
        demo_mode=True,
    )
    assert out["searched_regions"] == ["us-central1"]
    assert out["candidates"]
    assert all(c["region"] == "us-central1" for c in out["candidates"])


def test_the_tool_echoes_the_constraints_it_applied():
    out = _call_tool(
        workload_id="wl-mcp-echo",
        cpu_requested=8,
        allowed_regions=THREE,
        allow_spot=False,
        demo_mode=True,
    )
    assert out["allowed_regions"] == THREE
    assert out["allow_region_change"] is False


def test_the_custom_route_reports_the_same_metadata():
    from starlette.requests import Request

    async def _receive():
        import json

        body = json.dumps(
            {
                "cpu_requested": 8,
                "allowed_regions": ["europe-west4", "europe-west1"],
                "allow_spot": False,
                "demo_mode": True,
            }
        ).encode()
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/search_capacity",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    request = Request(scope, receive=_receive)

    import asyncio
    import json

    response = asyncio.run(mcp_server.search_capacity_route(request))
    body = json.loads(response.body)
    assert sorted(body["searched_regions"]) == ["europe-west1", "europe-west4"]
    assert "location_note" in body
    assert sorted({c["region"] for c in body["candidates"]}) == [
        "europe-west1",
        "europe-west4",
    ]


def test_the_tool_exposes_the_same_location_controls_as_the_http_route():
    """Signature drift is how 2-B happened; pin it."""
    params = set(inspect.signature(mcp_server.search_capacity).parameters)
    for name in ("allowed_regions", "target_region", "allow_region_change"):
        assert name in params, (
            f"the agent cannot express '{name}', which the HTTP route accepts"
        )


def test_the_returned_keys_match_the_http_route():
    out = _call_tool(workload_id="wl-keys", cpu_requested=4, demo_mode=True)
    for key in (
        "candidates",
        "searched_regions",
        "allowed_regions",
        "allow_region_change",
        "location_note",
    ):
        assert key in out, f"{key} missing from the MCP answer: {sorted(out)}"
