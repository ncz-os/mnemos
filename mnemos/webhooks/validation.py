"""Back-compat re-export — the webhook/SSRF URL validator moved to
``mnemos.core.net_validation`` (core layer) so ``core.safe_http`` can use it
without breaking the core→webhooks import contract. Import from
``mnemos.core.net_validation`` in new code.
"""

from __future__ import annotations

from mnemos.core.net_validation import (  # noqa: F401
    _BLOCKED_METADATA_HOSTS,
    _is_blocked_ip,
    _resolve_addrs,
    ValidatedWebhookURL,
    validate_webhook_url,
)

__all__ = ["ValidatedWebhookURL", "validate_webhook_url"]
