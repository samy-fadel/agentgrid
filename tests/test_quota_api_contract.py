"""The quota read must match the documented Compute Engine contract.

Two official references were checked before writing this:

* https://cloud.google.com/compute/docs/reference/rest/v1/regions/get
  ``quotas[]`` entries are ``{metric (enum), limit (number), usage (number),
  owner (string)}``. ``quotaStatusWarning`` is an *object*
  ``{code, message, data[{key, value}]}``, populated only when fetching the
  quotas field failed. The method **fails open**: it returns 200 with no quota
  data unless the organisation policy constraint
  ``compute.requireBasicQuotaInResponse`` is enforced.
* https://cloud.google.com/compute/docs/reference/rest/v1/projects/get
  Same ``quotas[]`` shape, and this is where the project-wide metrics live.

Reproduced defect: ``GPUS_ALL_REGIONS`` was listed among the *regional* metric
names. It is a project-wide ceiling and is never returned by ``regions.get``, so
it was never checked at all. A project with ``NVIDIA_L4_GPUS = 8`` regionally and
``GPUS_ALL_REGIONS = 0`` -- the common default on a new project -- was told:

    {"status": "QUOTA_AVAILABLE", "is_known": true,
     "reason": "Request fits within quota (verified against live Compute Engine
                regional quota)"}

Every VM creation for that request would have failed.
"""

from __future__ import annotations

import pytest

from agentic_compute import capacity_advisor as ca

#: Captured before the autouse fixture replaces them, so the transport tests
#: below can exercise the real readers.
_REAL_REGIONAL = ca._fetch_regional_quotas
_REAL_GLOBAL = ca._fetch_global_quotas

REGIONAL_OK = {
    "status": "ok",
    "quotas": {
        "CPUS": {"limit": 512.0, "usage": 0.0},
        "NVIDIA_L4_GPUS": {"limit": 8.0, "usage": 0.0},
    },
}


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    """Never reach the network from a test; each test states what the API said."""
    monkeypatch.setattr(ca, "_fetch_regional_quotas", lambda p, r: dict(REGIONAL_OK))
    monkeypatch.setattr(
        ca,
        "_fetch_global_quotas",
        lambda p: {"status": "ok", "quotas": {"GPUS_ALL_REGIONS": {"limit": 64.0, "usage": 0.0}}},
    )
    monkeypatch.delenv("GCP_QUOTA_CPU_LIMIT", raising=False)
    monkeypatch.delenv("GCP_PROJECT_QUOTA_CPUS", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)


def _global(monkeypatch, payload):
    monkeypatch.setattr(ca, "_fetch_global_quotas", lambda p: payload)


def test_the_global_gpu_ceiling_is_checked_not_just_the_regional_one(monkeypatch):
    """The exact reproduction: regional room, zero global ceiling."""
    _global(monkeypatch, {"status": "ok", "quotas": {"GPUS_ALL_REGIONS": {"limit": 0.0, "usage": 0.0}}})

    res = ca.check_quota_availability("proj", "europe-west4", cpu_needed=24, gpu_needed=4)

    assert res["status"] == "QUOTA_EXCEEDED"
    assert res["is_exceeded"] is True
    assert "GPUS_ALL_REGIONS" in res["reason"]


def test_a_partially_consumed_global_ceiling_is_respected(monkeypatch):
    _global(
        monkeypatch,
        {"status": "ok", "quotas": {"GPUS_ALL_REGIONS": {"limit": 16.0, "usage": 14.0}}},
    )

    res = ca.check_quota_availability("proj", "europe-west4", cpu_needed=24, gpu_needed=4)

    assert res["status"] == "QUOTA_EXCEEDED"
    assert "available 2/16" in res["reason"]


def test_a_request_that_clears_both_ceilings_is_authorised(monkeypatch):
    _global(
        monkeypatch,
        {"status": "ok", "quotas": {"GPUS_ALL_REGIONS": {"limit": 16.0, "usage": 0.0}}},
    )

    res = ca.check_quota_availability("proj", "europe-west4", cpu_needed=24, gpu_needed=4)

    assert res["status"] == "QUOTA_AVAILABLE"
    assert res["is_known"] is True


def test_an_unreadable_global_ceiling_is_unknown_not_available(monkeypatch):
    """'We could not check' must never be reported as 'quota is fine'."""
    _global(monkeypatch, {"status": "unavailable", "reason": "no credentials"})

    res = ca.check_quota_availability("proj", "europe-west4", cpu_needed=24, gpu_needed=4)

    assert res["status"] == "QUOTA_UNKNOWN"
    assert res["is_known"] is False
    assert res["is_exceeded"] is False
    assert "no credentials" in res["reason"]


def test_a_missing_global_metric_is_unknown(monkeypatch):
    _global(monkeypatch, {"status": "ok", "quotas": {"CPUS_ALL_REGIONS": {"limit": 500.0, "usage": 0.0}}})

    res = ca.check_quota_availability("proj", "europe-west4", cpu_needed=24, gpu_needed=4)

    assert res["status"] == "QUOTA_UNKNOWN"
    assert "GPUS_ALL_REGIONS" in res["reason"]


def test_a_cpu_only_request_does_not_read_the_global_quota(monkeypatch):
    """No GPU, no second API call: the ceiling is irrelevant."""
    calls: list[str] = []
    monkeypatch.setattr(
        ca, "_fetch_global_quotas", lambda p: calls.append(p) or {"status": "ok", "quotas": {}}
    )

    res = ca.check_quota_availability("proj", "europe-west4", cpu_needed=24, gpu_needed=0)

    assert res["status"] == "QUOTA_AVAILABLE"
    assert calls == []


def test_gpus_all_regions_is_not_treated_as_a_regional_metric():
    """It never appears in regions.get, so listing it there checked nothing."""
    assert "GPUS_ALL_REGIONS" not in ca._GPU_QUOTA_METRICS
    assert ca._GLOBAL_GPU_QUOTA_METRIC == "GPUS_ALL_REGIONS"


# ----------------------------------------------------------------------------
# Response parsing against the documented shapes.
# ----------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _stub_transport(monkeypatch, payload, status_code=200):
    # Undo the autouse stubs: these tests are about the readers themselves.
    monkeypatch.setattr(ca, "_fetch_regional_quotas", _REAL_REGIONAL)
    monkeypatch.setattr(ca, "_fetch_global_quotas", _REAL_GLOBAL)
    monkeypatch.setattr(ca, "HAVE_GOOGLE_AUTH", True)

    class _Creds:
        valid = True
        token = "tok"

    monkeypatch.setattr(ca.google.auth, "default", lambda scopes: (_Creds(), "proj"))
    monkeypatch.setattr(ca.requests, "get", lambda url, **kw: _Resp(payload, status_code))


def test_a_documented_quota_entry_is_parsed(monkeypatch):
    _stub_transport(
        monkeypatch,
        {
            "quotas": [
                {"metric": "CPUS", "limit": 128.0, "usage": 8.0, "owner": "regions/europe-west4"},
            ]
        },
    )

    out = ca._fetch_regional_quotas("proj", "europe-west4")

    assert out["status"] == "ok"
    assert out["quotas"]["CPUS"] == {"limit": 128.0, "usage": 8.0}


def test_the_documented_fail_open_response_is_reported_as_unavailable(monkeypatch):
    """200 with no quotas field is the documented default behaviour."""
    _stub_transport(monkeypatch, {})

    out = ca._fetch_regional_quotas("proj", "europe-west4")

    assert out["status"] == "unavailable"
    assert "no quota data" in out["reason"]


def test_the_quota_status_warning_object_is_rendered_not_repr_ed(monkeypatch):
    """It is {code, message, data[]}; interpolating it printed a dict repr."""
    _stub_transport(
        monkeypatch,
        {
            "quotaStatusWarning": {
                "code": "QUOTA_INFO_UNAVAILABLE",
                "message": "Quota information is temporarily unavailable.",
                "data": [{"key": "region", "value": "europe-west4"}],
            }
        },
    )

    out = ca._fetch_regional_quotas("proj", "europe-west4")

    assert out["status"] == "unavailable"
    assert "QUOTA_INFO_UNAVAILABLE" in out["reason"]
    assert "Quota information is temporarily unavailable." in out["reason"]
    assert "{" not in out["reason"]


def test_a_warning_alongside_data_is_surfaced(monkeypatch):
    """The warning is documented as appearing only when the read failed."""
    _stub_transport(
        monkeypatch,
        {
            "quotas": [{"metric": "CPUS", "limit": 128.0, "usage": 0.0}],
            "quotaStatusWarning": {"code": "PARTIAL", "message": "partial data"},
        },
    )
    out = ca._fetch_regional_quotas("proj", "europe-west4")
    assert out["warning"] == "PARTIAL: partial data"

    res = ca.check_quota_availability("proj", "europe-west4", cpu_needed=8, gpu_needed=0)
    assert res["quota_status_warning"] == "PARTIAL: partial data"


def test_the_global_read_targets_the_projects_endpoint(monkeypatch):
    """projects.get, not regions.get: that is where GPUS_ALL_REGIONS lives."""
    seen: list[str] = []
    monkeypatch.setattr(ca, "_fetch_global_quotas", _REAL_GLOBAL)
    monkeypatch.setattr(ca, "HAVE_GOOGLE_AUTH", True)

    class _Creds:
        valid = True
        token = "tok"

    monkeypatch.setattr(ca.google.auth, "default", lambda scopes: (_Creds(), "proj"))
    monkeypatch.setattr(
        ca.requests,
        "get",
        lambda url, **kw: seen.append(url) or _Resp({"quotas": [
            {"metric": "GPUS_ALL_REGIONS", "limit": 4.0, "usage": 0.0}
        ]}),
    )

    out = ca._fetch_global_quotas("my-project")

    assert out["quotas"]["GPUS_ALL_REGIONS"] == {"limit": 4.0, "usage": 0.0}
    assert seen == [
        "https://compute.googleapis.com/compute/v1/projects/my-project?fields=quotas"
    ]


def test_no_project_id_is_unavailable_not_an_exception():
    assert _REAL_GLOBAL("")["status"] == "unavailable"
    assert _REAL_REGIONAL("", "europe-west4")["status"] == "unavailable"
