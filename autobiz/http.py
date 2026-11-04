"""带限速与重试的共享 HTTP 客户端。"""
import time
from urllib.parse import urlparse

import requests

from .config import CFG

_session = requests.Session()
_session.headers.update({
    "User-Agent": CFG["app"]["user_agent"],
    "Accept-Language": "en-US,en;q=0.9",
})
_last_call: dict = {}
DEFAULT_TIMEOUT = CFG["crawl"]["timeout"]


def get(url: str, params: dict = None, timeout: int = None,
        retries: int = 3, min_interval: float = 1.0,
        headers: dict = None) -> requests.Response:
    host = urlparse(url).netloc
    last_err = None
    for attempt in range(retries):
        gap = _last_call.get(host, 0.0) + min_interval - time.time()
        if gap > 0:
            time.sleep(gap)
        try:
            r = _session.get(url, params=params,
                             timeout=timeout or DEFAULT_TIMEOUT, headers=headers)
            _last_call[host] = time.time()
            if r.status_code == 429 or r.status_code >= 500:
                last_err = RuntimeError(f"HTTP {r.status_code} for {url}")
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(2.0 * (attempt + 1))
    raise last_err if last_err else RuntimeError(f"failed: {url}")


def post_json(url: str, body: dict, headers: dict = None,
              timeout: int = 30, auth=None) -> requests.Response:
    r = _session.post(url, json=body, headers=headers,
                      timeout=timeout, auth=auth)
    r.raise_for_status()
    return r
