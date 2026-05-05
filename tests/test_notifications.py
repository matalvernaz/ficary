"""Tests for the watchlist notification dispatcher.

The bulk of the dispatcher is exercised end-to-end through the
watchlist tests; this module covers the redaction helper that sits
in front of every error-message construction so a Discord webhook
URL — which IS the credential — never lands in a log file.
"""

from __future__ import annotations

from urllib import error as urlerror

import pytest

from ffn_dl import notifications


def test_safe_endpoint_label_redacts_discord_webhook():
    """Discord webhook URLs end ``/webhooks/<id>/<token>`` and the
    token is the publish credential. The label must not echo the
    token in the part of the URL it returns."""
    secret = "T0PSECRET-token-do-not-leak"
    url = f"https://discord.com/api/webhooks/123456789/{secret}"
    label = notifications._safe_endpoint_label(url)
    assert secret not in label
    assert "discord.com" in label
    assert "Discord webhook" in label


def test_safe_endpoint_label_keeps_pushover_host():
    """Pushover's POST URL is a fixed endpoint with no embedded
    secret, so the host on its own is fine to include in errors."""
    label = notifications._safe_endpoint_label(notifications.PUSHOVER_ENDPOINT)
    assert "api.pushover.net" in label


def test_safe_endpoint_label_handles_garbage_input():
    """Some callers pass through whatever the user typed into the
    config field; the helper has to be robust against malformed URLs
    so a config error doesn't itself raise from the error path."""
    assert notifications._safe_endpoint_label("") == "<endpoint>"
    assert notifications._safe_endpoint_label("not-a-url") == "<endpoint>"


def test_post_redacts_webhook_in_urlerror_message(monkeypatch):
    """End-to-end: when ``_post`` translates a network failure into
    a NotificationError, the message must use the redacted label
    rather than the full URL with the embedded webhook token."""
    secret = "another-token-shhh"
    webhook = f"https://discord.com/api/webhooks/42/{secret}"

    def boom(*_args, **_kwargs):
        raise urlerror.URLError("name resolution failed")

    monkeypatch.setattr(notifications.urlrequest, "urlopen", boom)
    with pytest.raises(notifications.NotificationError) as excinfo:
        notifications._post(
            webhook,
            data=b"{}",
            content_type="application/json",
            timeout=1.0,
        )
    assert secret not in str(excinfo.value)
