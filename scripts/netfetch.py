"""
One HTTP layer for every script on the desk.

WHY THIS EXISTS
---------------
There were five `_get` functions across scripts/, and they disagreed about
everything that matters when a free endpoint has a bad minute:

  fund_nav.py       no retry at all. One blip and the fund printed
                    "would not price" and kept yesterday's figures for a day.
  market_series.py  no retry. One blip and an index dropped out of the chart.
  index_movers.py   three immediate attempts, no pause between them, every
                    exception swallowed the same way - so a 429 was answered
                    by two more requests inside a few milliseconds, which is
                    the one response guaranteed to extend a throttle.
  market_data.py    no retry, and a flat 0.3s sleep whether or not anything
                    had just been asked of the host.
  hl_factsheet.py   the only one that got it right: 404 is a wrong slug and
                    not retried, everything else backs off. That behaviour is
                    generalised here.

None of them honoured Retry-After, and none of them knew what any of the
others were doing - so eight index_movers threads and a fund_nav loop could
hit query1.finance.yahoo.com at once with nothing coordinating them.

WHAT THIS PROVIDES
------------------
  * retry with exponential backoff and jitter, on the statuses that mean
    "ask again later" (429, 5xx, and a transport failure) and never on the
    ones that mean "you asked for the wrong thing" (404, 400, 401, 403).
  * Retry-After honoured when the host sends one, capped so a hostile value
    cannot park a workflow for an hour.
  * a per-host rate limit shared across threads, so politeness is a property
    of the process rather than of whichever loop happens to be running.
  * one place to read a failure from, in words - `describe()`. An HTTP 429
    and a moved endpoint used to look identical in the run log.
  * `fetch_all()`, bounded concurrency that returns results in the order the
    inputs were given, so parallelising a loop cannot reorder a log.

Standard library only, like everything else here.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# A browser string for the sites that refuse anything else (HL, Wikipedia),
# and an honest one for the endpoints that do not care. Keeping both here
# stops a third variant appearing the next time a script is written.
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
UA_DESK = "fund-tracker/1.0 (+github actions; personal research desk)"

DEFAULT_TIMEOUT = 25
DEFAULT_TRIES = 3

# Statuses that mean "come back later". Everything else is a fact about the
# request, and repeating it just spends someone else's capacity to be told
# the same thing again.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Retry-After is a hint from the host, not an instruction to obey without
# limit. A run that parks for ten minutes on one symbol has failed anyway.
MAX_RETRY_AFTER = 30.0

# Requests per second per host, shared across every thread in the process.
# Yahoo's chart endpoint tolerates a steady stream and objects to bursts;
# HL and Wikipedia are someone's website rather than an API, and are asked
# far less often.
RATE: dict[str, float] = {
    "query1.finance.yahoo.com": 8.0,
    "query2.finance.yahoo.com": 8.0,
    "feeds.finance.yahoo.com": 4.0,
    "stooq.com": 4.0,
    "markets.ft.com": 2.0,
    "en.wikipedia.org": 2.0,
    "www.hl.co.uk": 1.0,
}
DEFAULT_RATE = 4.0

_lock = threading.Lock()
_next_free: dict[str, float] = {}


def _wait_turn(host: str) -> None:
    """Block until this host may be asked again. Thread-safe.

    The slot is claimed ONCE, under the lock, and then slept out. The
    obvious alternative - sleep, then loop round and re-check - claims a
    fresh slot on every pass, so each wait pushes the deadline another gap
    into the future and the caller never gets a turn at all. That version
    was written here first and hung on its own self-test.
    """
    gap = 1.0 / RATE.get(host, DEFAULT_RATE)
    with _lock:
        now = time.monotonic()
        ready = max(now, _next_free.get(host, 0.0))
        _next_free[host] = ready + gap
    if ready > now:
        time.sleep(ready - now)


def _penalise(host: str, seconds: float) -> None:
    """Hold every thread off a host that has just asked us to slow down."""
    with _lock:
        until = time.monotonic() + seconds
        if until > _next_free.get(host, 0.0):
            _next_free[host] = until


def describe(exc: BaseException) -> str:
    """Render a failed fetch as one readable line.

    HTTPError subclasses URLError, so catching URLError alone collapses a 429,
    a 404 and a DNS failure into the same silent None - which is what made a
    whole-panel failure unreadable from a run log. The distinction is the
    diagnosis: a status code means the host answered and refused us, which is
    waited out, while an unreachable host or an unparseable body means
    something moved and the code has to follow.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, TimeoutError):
            return "timed out"
        return f"unreachable ({exc.reason})"
    if isinstance(exc, TimeoutError):
        return "timed out"
    return f"{type(exc).__name__}: {exc}"


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Seconds the host asked us to wait, if it named a sane number."""
    # `if exc.headers` is wrong here and was written that way first: a
    # header collection is a mapping, and an EMPTY one is falsy, so the test
    # doubles carrying a single Retry-After were skipped entirely. Presence
    # is the question, not emptiness.
    hdrs = getattr(exc, "headers", None)
    if hdrs is None:
        return None
    try:
        raw = hdrs.get("Retry-After")
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return max(0.0, min(MAX_RETRY_AFTER, float(str(raw).strip())))
    except ValueError:
        return None          # the HTTP-date form; the backoff covers it


def _backoff(attempt: int) -> float:
    """0.6s, 1.8s, 5.4s ... with jitter, so parallel retries do not resonate.

    Without the jitter, eight threads throttled by the same 429 all sleep the
    same interval and hit the host again in the same millisecond - which is
    how a momentary throttle becomes a sustained one.
    """
    return (0.6 * (3 ** attempt)) * (0.75 + random.random() * 0.5)


class Result:
    """What one fetch produced. Falsy when it produced nothing.

    A bare `bytes | None` cannot say WHY, and the reason is what a run log is
    read for. Callers that only want the body can use `or b""`, or `.body`.
    """

    __slots__ = ("url", "body", "status", "error")

    def __init__(self, url: str, body: bytes | None,
                 status: int | None = None, error: str | None = None):
        self.url, self.body, self.status, self.error = url, body, status, error

    def __bool__(self) -> bool:
        return self.body is not None

    @property
    def text(self) -> str:
        return (self.body or b"").decode("utf-8", "replace")

    def json(self):
        """Parsed body, or None if it is absent or not JSON."""
        if self.body is None:
            return None
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, ValueError):
            self.error = f"{len(self.body)} bytes that did not parse as JSON"
            return None


def fetch(url: str, *, headers: dict | None = None, ua: str = UA_DESK,
          timeout: int = DEFAULT_TIMEOUT, tries: int = DEFAULT_TRIES,
          _opener=None) -> Result:
    """GET a URL, retrying what is worth retrying. Never raises."""
    host = urllib.parse.urlsplit(url).hostname or ""
    hdrs = {"User-Agent": ua, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    opener = _opener or urllib.request.urlopen
    last = "no attempt made"
    status = None

    for attempt in range(max(1, tries)):
        _wait_turn(host)
        try:
            with opener(req, timeout=timeout) as resp:
                return Result(url, resp.read(), getattr(resp, "status", 200))
        except urllib.error.HTTPError as exc:
            status, last = exc.code, describe(exc)
            if exc.code not in RETRY_STATUS:
                return Result(url, None, status, last)
            pause = _retry_after(exc)
            if pause is not None:
                # The host named a number. Hold every thread off it, not just
                # this one - a 429 is about the process, not the request.
                _penalise(host, pause)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = describe(exc)
        if attempt < tries - 1:
            time.sleep(_backoff(attempt))
    return Result(url, None, status, last)


def fetch_json(url: str, **kw):
    """Parsed JSON, or None. The reason is on the Result if one is needed."""
    return fetch(url, **kw).json()


def fetch_all(urls, *, workers: int = 6, **kw) -> list[Result]:
    """Fetch several URLs concurrently, RESULTS IN INPUT ORDER.

    Order is the point. Parallelising a loop that prints as it goes otherwise
    shuffles the run log run to run, and a log you cannot diff is a log you
    stop reading. The rate limiter above is what keeps the concurrency
    polite; the worker count only decides how much latency is hidden.
    """
    urls = list(urls)
    if not urls:
        return []
    if len(urls) == 1 or workers <= 1:
        return [fetch(u, **kw) for u in urls]
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        return list(pool.map(lambda u: fetch(u, **kw), urls))


def chunked(items, size: int) -> list[list]:
    """Split a list into runs of at most `size`, for batch endpoints."""
    items = list(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Self-test - runs offline against a stub opener
# ---------------------------------------------------------------------------
def _selftest() -> bool:
    import io as _io
    import sys as _sys

    class _Resp(_io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener_for(script):
        """Replay a list of outcomes; record how many attempts were made."""
        calls = []

        def _open(req, timeout=None):
            calls.append(req.full_url)
            outcome = script[min(len(calls) - 1, len(script) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            return _Resp(outcome)
        _open.calls = calls
        return _open

    # A 404 is a fact about the request. Repeating it cannot change it, and
    # doing so three times per wrong slug is what hl_factsheet.py already
    # knew to avoid - now everything does.
    op = opener_for([urllib.error.HTTPError("u", 404, "Not Found", {}, None)])
    r = fetch("https://example.test/a", tries=3, _opener=op)
    assert not r and r.status == 404 and len(op.calls) == 1, op.calls
    assert r.error == "HTTP 404 Not Found", r.error

    # 403 likewise - a blocked request is not a busy one.
    op = opener_for([urllib.error.HTTPError("u", 403, "Forbidden", {}, None)])
    fetch("https://example.test/b", tries=3, _opener=op)
    assert len(op.calls) == 1, "403 must not be retried"

    # A 429 is. And the body from the retry is the one that comes back.
    op = opener_for([urllib.error.HTTPError("u", 429, "Too Many", {}, None),
                     b'{"ok":true}'])
    r = fetch("https://example.test/c", tries=3, _opener=op)
    assert r and r.json() == {"ok": True}, (r.body, r.error)
    assert len(op.calls) == 2, op.calls

    # So is a transport failure, and the last reason is kept when it runs out.
    op = opener_for([urllib.error.URLError(TimeoutError())])
    r = fetch("https://example.test/d", tries=2, _opener=op)
    assert not r and r.error == "timed out", r.error
    assert len(op.calls) == 2, op.calls
    print("  retry policy     OK  (404/403 once, 429 and timeout retried)",
          file=_sys.stderr)

    # Retry-After is read, clamped, and applied to the HOST rather than to
    # this one request - a throttle is about the process, not the call.
    def _throttled(value):
        return urllib.error.HTTPError("u", 429, "Too Many",
                                      {"Retry-After": value}, None)
    assert _retry_after(_throttled("2")) == 2.0
    assert _retry_after(_throttled("9999")) == MAX_RETRY_AFTER
    # The HTTP-date form is legal and not parsed here; the backoff covers it.
    assert _retry_after(_throttled("Wed, 21 Oct 2026 07:28:00 GMT")) is None
    # An empty header collection is falsy but present, which is why presence
    # is tested with `is None` rather than truthiness.
    assert _retry_after(urllib.error.HTTPError("u", 429, "T", {}, None)) is None
    assert _retry_after(urllib.error.HTTPError("u", 429, "T", None, None)) is None
    print("  retry-after      OK  (read, clamped, HTTP-date ignored)",
          file=_sys.stderr)

    # The rate limiter is what makes concurrency polite. Six requests at a
    # nominal 20/s must take at least five gaps between them.
    RATE["ratetest.invalid"] = 20.0
    _next_free.pop("ratetest.invalid", None)
    op = opener_for([b"x"])
    began = time.monotonic()
    fetch_all([f"https://ratetest.invalid/{i}" for i in range(6)],
              workers=6, _opener=op)
    spent = time.monotonic() - began
    assert spent >= 5 / 20.0 * 0.8, f"rate limit not applied: {spent:.3f}s"
    print(f"  rate limit       OK  (6 calls at 20/s took {spent*1000:.0f}ms, "
          "shared across threads)", file=_sys.stderr)

    # Order is the reason fetch_all exists rather than a bare pool.map with
    # completion order. A parallel log must diff against a sequential one.
    def _echo(req, timeout=None):
        n = int(req.full_url.rsplit("/", 1)[1])
        time.sleep((7 - n) * 0.004)     # later inputs finish first
        return _Resp(str(n).encode())
    got = fetch_all([f"https://ratetest.invalid/{i}" for i in range(7)],
                    workers=7, _opener=_echo)
    assert [r.text for r in got] == [str(i) for i in range(7)], \
        [r.text for r in got]
    assert fetch_all([], workers=4, _opener=_echo) == []
    print("  ordering         OK  (slowest-first inputs still come back "
          "in order)", file=_sys.stderr)

    # A body that is not JSON is a failure with a reason, not a crash.
    op = opener_for([b"<html>nope</html>"])
    r = fetch("https://example.test/e", _opener=op)
    assert r and r.json() is None and "did not parse" in r.error, r.error
    assert Result("u", None).json() is None

    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunked([], 10) == []
    assert chunked([1], 10) == [[1]]
    print("  parsing/chunking OK", file=_sys.stderr)

    # Backoff grows and stays inside its jitter band, so a slow host is not
    # hammered and a workflow is not parked.
    # 0.6s, 1.8s, 5.4s, each multiplied by jitter in [0.75, 1.25].
    for _ in range(200):
        assert 0.45 <= _backoff(0) <= 0.75
        assert 1.35 <= _backoff(1) <= 2.25
        assert 4.05 <= _backoff(2) <= 6.75
    assert len({_backoff(0) for _ in range(20)}) > 1, "jitter must vary"
    print("  backoff          OK  (0.6/1.8/5.4s, jittered)", file=_sys.stderr)

    print("  netfetch self-test: OK", file=_sys.stderr)
    return True


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit("nothing to run - this is a library. Try --selftest.")
