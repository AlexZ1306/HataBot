from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_session(*, user_agent: str, retries: int = 2, backoff_factor: float = 0.6) -> requests.Session:
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        allowed_methods=frozenset({"GET", "POST"}),
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=backoff_factor,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

