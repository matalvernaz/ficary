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


# ── Rate-limiter regression ────────────────────────────────────────


def test_dispatch_paces_burst_per_channel(monkeypatch):
    """A watchlist update producing many events in one tick must
    pace its sends so Discord/Pushover don't 429. Regression: an
    earlier shape fired every event back-to-back in a tight loop.
    """
    # Reset the module-level send-time tracker so prior tests don't
    # bleed minimum-interval credit into this one.
    monkeypatch.setattr(notifications, "_LAST_SEND_AT", {})
    sleeps: list[float] = []

    def record_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(notifications.time, "sleep", record_sleep)
    # Fake monotonic clock advances only when the test asks it to.
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        notifications.time, "monotonic", lambda: fake_now["t"],
    )

    # First call to a channel: no sleep, sets baseline.
    notifications._wait_for_channel_slot("discord")
    assert sleeps == []

    # Immediate second call (no clock advance): expect a sleep up to
    # the channel's minimum interval.
    notifications._wait_for_channel_slot("discord")
    assert sleeps and sleeps[-1] > 0
    assert sleeps[-1] <= notifications._CHANNEL_MIN_INTERVAL_S["discord"] + 1e-6

    # Pushover is paced independently from Discord — pushover's first
    # call doesn't wait for Discord's window.
    sleeps.clear()
    notifications._wait_for_channel_slot("pushover")
    assert sleeps == []  # first pushover send → no wait


def test_notification_url_title_is_settable():
    """Pushover senders must consume Notification.url_title rather
    than hardcoding "Open" — for accessibility, watch-type-specific
    labels ("Open story", "Open author") read better via NVDA."""
    n = notifications.Notification(
        title="t", message="m", url="https://x", url_title="Open story",
    )
    assert n.url_title == "Open story"

    sent: dict = {}

    def record(endpoint, fields, timeout=10):
        sent.update(fields)

    # send_pushover routes through _post_form; intercept that.
    import ffn_dl.notifications as nm

    real_post_form = nm._post_form
    try:
        nm._post_form = record
        nm.send_pushover("tok", "user", n)
    finally:
        nm._post_form = real_post_form
    assert sent.get("url_title") == "Open story"


def test_notification_url_title_default_is_open():
    """Backwards compatibility: callers that don't set url_title
    keep the old "Open" label rather than blowing up on a missing
    attribute."""
    n = notifications.Notification(title="t", message="m", url="https://x")
    assert n.url_title == "Open"
