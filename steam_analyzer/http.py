import logging
import time
from collections.abc import Callable

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "steam-review-sentiment-analysis-pipeline/0.1 "
    "(+https://github.com/dumly55/Steam-Review-Sentiment-Analysis-Pipeline)"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def _retry_delay(resp: requests.Response | None, attempt: int, backoff_seconds: float) -> float:
    retry_after = resp.headers.get("Retry-After", "") if resp is not None else ""
    if retry_after.isdigit():
        return float(retry_after)
    return backoff_seconds * 2 ** (attempt - 1)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_attempts: int = 4,
    backoff_seconds: float = 2.0,
    timeout: float = 30,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> requests.Response:
    """Send a request, retrying rate limits, server errors and network failures with backoff.

    Other HTTP errors (e.g. 403, 404) raise immediately.
    """
    for attempt in range(1, max_attempts + 1):
        resp = None
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            if attempt == max_attempts:
                raise
            log.warning("request failed (%s), attempt %d/%d: %s", url, attempt, max_attempts, exc)
        else:
            if resp.status_code not in RETRY_STATUSES:
                resp.raise_for_status()
                return resp
            if attempt == max_attempts:
                resp.raise_for_status()
            log.warning("HTTP %d from %s, attempt %d/%d", resp.status_code, url, attempt, max_attempts)
        sleep(_retry_delay(resp, attempt, backoff_seconds))
    raise AssertionError("unreachable")
