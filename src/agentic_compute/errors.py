"""Error taxonomy shared by every runtime adapter.

The distinction that matters
----------------------------
Before this module, `ExecutionController.submit_plan` wrapped the whole
submission in `except Exception` and treated *any* failure as a possibly-ambiguous
network timeout. It then went looking for "an active job" and reported success
when it found the unrelated demo workload.

A `TypeError` from passing an unsupported keyword is not a network timeout. A
rejected machine type is not a network timeout. Only a genuine transport failure
leaves the outcome unknown, and only that case justifies looking for a job that
may have been created.

These three classes make that distinction explicit and testable:

* :class:`ConfigurationRejected` -- the request was refused *before* anything was
  created. Deterministic; retrying it unchanged will fail again. Never ambiguous.
* :class:`AmbiguousSubmission` -- the transport failed at a point where the
  scheduler may or may not have accepted the job. The outcome is genuinely
  unknown and must be resolved by looking up a stable submission identity.
* :class:`RuntimeUnavailable` -- the backing runtime could not be reached at all,
  and no silent substitute may be used in its place.
"""
from __future__ import annotations


class AgentGridRuntimeError(RuntimeError):
    """Base class for runtime adapter failures."""


class ConfigurationRejected(AgentGridRuntimeError, ValueError):
    """The runtime refused the request; no resource was created.

    Covers unsupported machine types, invalid provisioning models, requests
    exceeding cluster capacity, unknown workload ids, and programming errors
    such as unsupported keyword arguments. These must never be retried by
    looking for a job that might exist -- no job exists.

    Also subclasses :class:`ValueError` because adapters have historically
    raised ``ValueError`` for rejected parameters, and callers (and tests) rely
    on that. Inheriting from both keeps those contracts intact while letting new
    code catch the precise type.
    """

    def __init__(self, message: str, *, parameter: str | None = None) -> None:
        super().__init__(message)
        self.parameter = parameter


class AmbiguousSubmission(AgentGridRuntimeError):
    """Transport failed after the request may already have been accepted.

    Carries the stable ``submission_key`` so the caller can look for that exact
    submission instead of accepting any job it happens to find.
    """

    def __init__(self, message: str, *, submission_key: str | None = None) -> None:
        super().__init__(message)
        self.submission_key = submission_key


class RuntimeUnavailable(AgentGridRuntimeError):
    """The configured runtime is unreachable and no substitute is permitted.

    Raised instead of silently falling back to a local simulator, which would
    present simulated state as though it came from the real cluster.
    """
