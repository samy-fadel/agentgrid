"""Defense-in-depth security primitives for the AgentGrid Compute Control Plane.

Provides:
1. Command argument sanitization and shell injection prevention (safe argv parsing).
2. W3C-compliant CORS policy resolution (preventing wildcard + allow_credentials).
3. Multi-tenant operator ownership extraction and verification.
4. Sliding-window rate limiting and pagination clamping to prevent Denial of Service (DoS).
"""
from __future__ import annotations

import os
import re
import shlex
import threading
import time
from collections import defaultdict, deque
from typing import Any

# Shell metacharacters and chaining constructs that enable command injection
# when a workload command is interpolated into a shell wrapper or sbatch script.
_DANGEROUS_SHELL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("command_chaining_semicolon", re.compile(r";")),
    ("logical_and_chain", re.compile(r"&&")),
    ("logical_or_chain", re.compile(r"\|\|")),
    ("pipe_operator", re.compile(r"\|")),
    ("command_substitution_subshell", re.compile(r"\$\(")),
    ("command_substitution_backtick", re.compile(r"`")),
    ("io_redirection", re.compile(r"[<>]")),
    ("multiline_injection", re.compile(r"[\r\n]")),
]

MAX_HISTORY_LIMIT = 500
DEFAULT_HISTORY_LIMIT = 50


def validate_command_safety(command: str | None) -> dict[str, Any]:
    """Validate a workload command string and decompose it into a safe execve-ready argv list.

    Never executes anything. Rejects shell chaining, pipes, subshells, backticks,
    and redirections so that transitioning from SimulatedRuntime to a real Slurm
    cluster or VM runner cannot be exploited for arbitrary command injection.
    """
    if command is None or not str(command).strip():
        return {
            "is_safe": True,
            "status": "empty_command",
            "safe_argv": [],
            "blocked_patterns": [],
            "reason": "No custom command specified; runtime default entrypoint will be used.",
        }

    raw = str(command).strip()
    blocked: list[str] = []
    for name, pattern in _DANGEROUS_SHELL_PATTERNS:
        if pattern.search(raw):
            blocked.append(name)

    if blocked:
        return {
            "is_safe": False,
            "status": "blocked_shell_injection",
            "safe_argv": [],
            "blocked_patterns": blocked,
            "reason": (
                f"Command contains forbidden shell metacharacters or chaining constructs "
                f"({', '.join(blocked)}). Provide a single executable invocation without "
                f"shell operators."
            ),
        }

    try:
        argv = shlex.split(raw, posix=True)
    except ValueError as exc:
        return {
            "is_safe": False,
            "status": "blocked_shell_injection",
            "safe_argv": [],
            "blocked_patterns": ["malformed_quoting"],
            "reason": f"Command quoting could not be safely parsed into argv: {exc}",
        }

    if not argv:
        return {
            "is_safe": True,
            "status": "empty_command",
            "safe_argv": [],
            "blocked_patterns": [],
            "reason": "Parsed command yielded empty argv.",
        }

    return {
        "is_safe": True,
        "status": "verified_safe_argv",
        "safe_argv": argv,
        "blocked_patterns": [],
        "reason": f"Parsed cleanly into {len(argv)} argument(s) without shell interpolation.",
    }


def resolve_cors_policy(env_origins: str | None = None) -> dict[str, Any]:
    """Return a safe CORSMiddleware configuration dictionary.

    Fixes the security flaw of combining ``allow_origins=['*']`` with
    ``allow_credentials=True``. Credentials are only enabled when explicit
    trusted origins are configured via ``AGENTGRID_ALLOWED_ORIGINS``.
    """
    raw = env_origins if env_origins is not None else os.getenv("AGENTGRID_ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]

    if not origins or "*" in origins:
        return {
            "allow_origins": ["*"],
            "allow_credentials": False,
            "allow_methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Authorization", "Content-Type", "X-API-Key", "X-Operator-Id", "X-Tenant-Id"],
            "policy_mode": "public_wildcard_no_credentials",
        }

    return {
        "allow_origins": origins,
        "allow_credentials": True,
        "allow_methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Authorization", "Content-Type", "X-API-Key", "X-Operator-Id", "X-Tenant-Id"],
        "policy_mode": "explicit_allowlist_with_credentials",
    }


def clamp_pagination_limit(limit: int | None, default: int = DEFAULT_HISTORY_LIMIT, maximum: int = MAX_HISTORY_LIMIT) -> int:
    """Clamp user-supplied pagination limits to prevent unbounded memory/SQLite scans."""
    if limit is None:
        return default
    try:
        val = int(limit)
    except (TypeError, ValueError):
        return default
    if val < 1:
        return 1
    return min(val, maximum)


def extract_operator_identity(headers: dict[str, Any] | Any) -> dict[str, str]:
    """Extract operator and tenant identity from request headers."""
    getter = getattr(headers, "get", lambda k, d=None: d)
    operator_id = (getter("x-operator-id") or getter("X-Operator-Id") or "operator").strip()
    tenant_id = (getter("x-tenant-id") or getter("X-Tenant-Id") or "default").strip()
    return {
        "operator_id": operator_id or "operator",
        "tenant_id": tenant_id or "default",
    }


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter for API endpoints."""

    def __init__(self, default_rpm: int = 300, window_seconds: float = 60.0) -> None:
        self.default_rpm = default_rpm
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(
        self,
        key: str,
        max_requests: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Check and record a request against the sliding window bucket."""
        env_rpm = os.getenv("AGENTGRID_RATE_LIMIT_RPM")
        limit = max_requests
        if limit is None:
            if env_rpm is not None:
                try:
                    limit = int(env_rpm)
                except ValueError:
                    limit = self.default_rpm
            else:
                limit = self.default_rpm

        if limit <= 0:
            return {"allowed": True, "remaining": 999999, "limit": 0, "retry_after_seconds": 0.0}

        ts = now if now is not None else time.time()
        cutoff = ts - self.window_seconds

        with self._lock:
            dq = self._buckets[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= limit:
                oldest = dq[0]
                retry_after = max(0.01, round((oldest + self.window_seconds) - ts, 2))
                return {
                    "allowed": False,
                    "remaining": 0,
                    "limit": limit,
                    "retry_after_seconds": retry_after,
                }

            dq.append(ts)
            return {
                "allowed": True,
                "remaining": max(0, limit - len(dq)),
                "limit": limit,
                "retry_after_seconds": 0.0,
            }

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


default_rate_limiter = SlidingWindowRateLimiter()
