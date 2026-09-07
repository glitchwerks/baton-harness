"""Fire-and-forget Slack webhook alert helper.

Provides a single public function ``post_slack_alert`` that POSTs a
JSON body to a Slack incoming-webhook URL.  The function NEVER raises —
any delivery failure is caught, logged at WARNING level, and returns
``False``.

Security note: the webhook URL contains a bearer-secret token in its
path (``/services/T.../B.../<SECRET>``).  Log messages must NEVER
include the raw URL — only the host is logged to avoid leaking the
secret into log aggregators.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger(__name__)


def post_slack_alert(
    url: str,
    message: str,
    *,
    timeout: float = 5.0,
) -> bool:
    """POST a Slack-contract JSON alert to a webhook URL.

    Fire-and-forget: this function NEVER raises.  Any exception
    (``URLError``, ``TimeoutError``, or otherwise) is caught, logged at
    WARNING level, and causes the function to return ``False``.

    Args:
        url: Slack incoming-webhook URL to POST to.
        message: Human-readable alert text to include in the ``"text"``
            field of the Slack payload.
        timeout: Request timeout in seconds.  Default ``5.0`` so a
            hung webhook cannot stall the daemon indefinitely.

    Returns:
        ``True`` on HTTP 2xx; ``False`` on non-2xx or any exception.
    """
    host = urllib.parse.urlparse(url).hostname or "<unparseable>"
    payload = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True
            _log.warning(
                "post_slack_alert: non-2xx response %d from host=%s",
                resp.status,
                host,
            )
            return False
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "post_slack_alert: delivery failed (host=%s): %s",
            host,
            exc,
        )
        return False
