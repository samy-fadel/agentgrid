from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def get_gcp_id_token(audience: str) -> Optional[str]:
    """Obtain a Google Cloud OIDC ID token for service-to-service invocation.

    When running in Cloud Run, this retrieves the token from the GCP metadata server.
    For local development or testing, it can be overridden with the GCP_ID_TOKEN
    environment variable.
    """
    # 1. Manual override for local testing / CI
    override = os.getenv("GCP_ID_TOKEN")
    if override:
        return override

    # 2. If disabled explicitly
    if os.getenv("DISABLE_GCP_AUTH", "").lower() in ("true", "1", "yes"):
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        auth_req = Request()
        token = id_token.fetch_id_token(auth_req, audience)
        return token
    except Exception as exc:
        logger.debug(
            "Could not fetch GCP ID token for audience %s (expected in non-GCP environments): %s",
            audience,
            exc,
        )
        return None
