import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import AmbiguousWriteError, ApiError, SyncError

LOG = logging.getLogger(__name__)
TRANSIENT = {408, 429, 500, 502, 503, 504}


def retry_delay(headers, attempt):
    headers = {str(k).lower(): v for k, v in headers.items()}
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (ValueError, TypeError):
            try:
                return max(0.0, (parsedate_to_datetime(retry_after)
                               - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    reset_ms = headers.get("x-rate-limit-time-reset-ms")
    if reset_ms is not None:
        try:
            return max(0.0, float(reset_ms) / 1000)
        except (ValueError, TypeError):
            pass
    return min(60, 2 ** attempt) + random.uniform(0, 1)


def error_code(body):
    try:
        code = json.loads(body).get("error", {})
        if isinstance(code, dict):
            code = code.get("code", "unknown")
        # Return only machine-readable codes; never log API bodies or customer data.
        return code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", code) else "unknown"
    except (ValueError, AttributeError, TypeError):
        return "unknown"


class HttpClient:
    def __init__(self, service, timeout=60, attempts=6, opener=urlopen, sleep=time.sleep):
        self.service = service
        self.timeout = timeout
        self.attempts = attempts
        self.opener = opener
        self.sleep = sleep

    def request(self, method, url, *, headers=None, body=None, retry_safe=True):
        headers = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body, allow_nan=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        for attempt in range(self.attempts):
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if not raw:
                        return None
                    try:
                        return json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        if not retry_safe:
                            raise AmbiguousWriteError(
                                f"{self.service}: invalid write response; rerun to reconcile") from None
                        raise SyncError(f"{self.service}: invalid JSON response") from None
            except HTTPError as error:
                status = error.code
                error_headers = dict(error.headers or {})
                try:
                    code = error_code(error.read())
                finally:
                    error.close()
                # An outer 429 is an explicit rejection. Timeouts/5xx on creates
                # are ambiguous; never replay them blindly.
                can_retry = status == 429 or (retry_safe and status in TRANSIENT)
                if can_retry and attempt + 1 < self.attempts:
                    delay = retry_delay(error_headers, attempt)
                    LOG.warning("%s HTTP %s; retry %s/%s in %.1fs",
                                self.service, status, attempt + 2, self.attempts, delay)
                    self.sleep(delay)
                    continue
                if not retry_safe and status in TRANSIENT - {429}:
                    raise AmbiguousWriteError(
                        f"{self.service}: ambiguous write HTTP {status}; rerun to reconcile") from None
                raise ApiError(self.service, status, code, error_headers) from None
            except (URLError, TimeoutError, ConnectionError, OSError):
                if not retry_safe:
                    raise AmbiguousWriteError(
                        f"{self.service}: connection lost during write; rerun to reconcile") from None
                if attempt + 1 >= self.attempts:
                    raise SyncError(f"{self.service}: network failure after {self.attempts} attempts") from None
                delay = retry_delay({}, attempt)
                LOG.warning("%s network failure; retry %s/%s in %.1fs",
                            self.service, attempt + 2, self.attempts, delay)
                self.sleep(delay)
        raise SyncError(f"{self.service}: retries exhausted")
