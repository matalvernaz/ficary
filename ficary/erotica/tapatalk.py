"""Tapatalk Groups (mobiquo) XML-RPC client for The Mousepad.

Tapatalk-hosted phpBB boards front their HTML pages with a Cloudflare
JS challenge, so ordinary scraping is a non-starter. Their own mobile
app, however, talks XML-RPC to ``/groups/<group>/mobiquo/mobiquo.php``
and that endpoint answers guests directly — full forum tree, windowed
topic listings, and windowed thread bodies with per-post author ids.
Everything ficary needs rides those three methods:

* ``get_topic(forum_id, start, end)`` — topic listing, newest activity
  first. The server honours the offset but caps each response at
  :data:`TOPIC_WINDOW` rows.
* ``get_thread(topic_id, start, end, return_html)`` — post bodies as
  HTML, windowed the same way.
* ``get_config`` — reachability probe (used by tests/doctor only).

The native ``search`` method is deliberately NOT used: as deployed on
tapatalk.com it ignores its offset parameter and returns at most the
five newest matches, so query searches instead title-filter the topic
listing (the standard pattern for erotica adapters without a real
search API).

Text fields come back base64-wrapped (``xmlrpc.client.Binary``) and
dates as ``xmlrpc.client.DateTime`` in a compact ``YYYYMMDDTHH:MM:SS``
form; :func:`decode_value` / :func:`iso_datetime` normalise both.
"""

import http.client
import logging
import socket
import time
import xmlrpc.client

logger = logging.getLogger(__name__)

MOUSEPAD_GROUP = "themousepad"
MOUSEPAD_BASE = f"https://www.tapatalk.com/groups/{MOUSEPAD_GROUP}"

# The app's UA. The mobiquo endpoint serves generic clients too, but
# matching the official app keeps us on the path Tapatalk actually
# maintains (and load-balances) for third-party boards.
TAPATALK_USER_AGENT = "Tapatalk/8.9.9"

REQUEST_TIMEOUT_S = 25

# Server-side response caps observed on tapatalk.com (July 2026):
# get_topic honours its start offset but never returns more than 50
# rows per call; get_thread windows behave the same way.
TOPIC_WINDOW = 50
THREAD_WINDOW = 50

_ATTEMPTS = 2
_RETRY_DELAY_S = 2.0


class _TapatalkTransport(xmlrpc.client.Transport):
    """Transport that sends the app UA and applies a socket timeout
    (``ServerProxy`` itself has no timeout parameter)."""

    user_agent = TAPATALK_USER_AGENT

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = REQUEST_TIMEOUT_S
        return conn


class _TapatalkSafeTransport(xmlrpc.client.SafeTransport):
    user_agent = TAPATALK_USER_AGENT

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = REQUEST_TIMEOUT_S
        return conn


def mobiquo_url(group: str = MOUSEPAD_GROUP) -> str:
    return f"https://www.tapatalk.com/groups/{group}/mobiquo/mobiquo.php"


def mobiquo_call(method: str, *params, group: str = MOUSEPAD_GROUP):
    """Invoke one mobiquo XML-RPC method as a guest and return the raw
    response struct.

    A fresh ``ServerProxy`` per call keeps this safe to use from the
    search fan-out's worker threads and a download running at the same
    time. Transient transport failures get one retry; XML-RPC ``Fault``
    responses are server-side answers and propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        proxy = xmlrpc.client.ServerProxy(
            mobiquo_url(group), transport=_TapatalkSafeTransport(),
        )
        try:
            return getattr(proxy, method)(*params)
        except (socket.error, http.client.HTTPException,
                xmlrpc.client.ProtocolError) as exc:
            last_exc = exc
            logger.warning(
                "mobiquo %s attempt %d/%d failed: %s",
                method, attempt, _ATTEMPTS, exc,
            )
            if attempt < _ATTEMPTS:
                time.sleep(_RETRY_DELAY_S)
    raise last_exc


def decode_value(value) -> str:
    """Unwrap mobiquo's base64 ``Binary`` text fields to ``str``;
    everything else passes through ``str()`` untouched semantics-wise."""
    if isinstance(value, xmlrpc.client.Binary):
        return value.data.decode("utf-8", "replace")
    if value is None:
        return ""
    return str(value)


def iso_datetime(value) -> str:
    """Normalise a mobiquo date to sortable ISO-8601, or ``""``.

    Accepts ``xmlrpc.client.DateTime`` (compact ``20260709T15:46:58``
    form, sometimes with a ``+00:00`` suffix) or a pre-decoded string.
    Output shape ``YYYY-MM-DDTHH:MM:SS`` sorts lexicographically and
    slices to a display date with ``[:10]``.
    """
    if isinstance(value, xmlrpc.client.DateTime):
        raw = value.value
    else:
        raw = decode_value(value)
    raw = raw.strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        date = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        time_part = raw[9:17] if len(raw) >= 17 and raw[8] == "T" else ""
        return f"{date}T{time_part}" if time_part else date
    return ""


def topic_url(topic_id) -> str:
    """Canonical story URL for a Mousepad topic. The phpBB
    ``viewtopic.php?t=N`` form is stable and slug-free, so it survives
    topic renames and round-trips through the scraper's URL parser."""
    return f"{MOUSEPAD_BASE}/viewtopic.php?t={int(str(topic_id))}"
