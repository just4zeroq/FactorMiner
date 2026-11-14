"""Lock the API auth + CORS fixes from the system code review (finding T6).

* ``allow_origins=["*"]`` must never be paired with ``allow_credentials=True``
  (browsers reject that combo). The credentials flag is now derived from the
  origin set: wildcard -> credentials off, explicit origins -> credentials on.
* When ``FACTORMINER_API_TOKEN`` is set, every HTTP endpoint requires it; the
  ``/api/health`` endpoint stays public; with no token the API is open (dev).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import unittest
from starlette.requests import Request

import api.main as main


def _make_request(path: str = "/api/stats", token: str | None = None) -> Request:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {"type": "http", "method": "GET", "path": path, "headers": headers}
    return Request(scope)


class TestCorsDerivation(unittest.TestCase):
    def test_default_wildcard_disables_credentials(self):
        # The old combo (allow_origins=["*"] + allow_credentials=True) is invalid.
        self.assertEqual(main.CORS_ORIGINS, ["*"])
        self.assertFalse(main._ALLOW_CREDENTIALS)

    def test_explicit_origins_keep_credentials(self):
        old = os.environ.get("FACTORMINER_CORS_ORIGINS")
        os.environ["FACTORMINER_CORS_ORIGINS"] = "http://localhost:3000,https://app.example.com"
        try:
            importlib.reload(main)
            self.assertEqual(
                main.CORS_ORIGINS, ["http://localhost:3000", "https://app.example.com"]
            )
            self.assertTrue(main._ALLOW_CREDENTIALS)
        finally:
            if old is None:
                os.environ.pop("FACTORMINER_CORS_ORIGINS", None)
            else:
                os.environ["FACTORMINER_CORS_ORIGINS"] = old
            importlib.reload(main)


class TestApiAuth(unittest.TestCase):
    def setUp(self):
        self._saved_token = main.API_TOKEN

    def tearDown(self):
        main.API_TOKEN = self._saved_token

    def test_open_when_no_token_configured(self):
        main.API_TOKEN = None
        # Must not raise even with no/wrong header.
        asyncio.run(main.require_auth(_make_request(token="anything")))
        asyncio.run(main.require_auth(_make_request()))

    def test_valid_token_passes(self):
        main.API_TOKEN = "s3cr3t"
        asyncio.run(main.require_auth(_make_request(token="s3cr3t")))

    def test_missing_or_wrong_token_rejected(self):
        main.API_TOKEN = "s3cr3t"
        for req in (_make_request(), _make_request(token="wrong")):
            with self.assertRaises(Exception):  # HTTPException (401)
                asyncio.run(main.require_auth(req))

    def test_health_stays_public_even_with_token(self):
        main.API_TOKEN = "s3cr3t"
        asyncio.run(main.require_auth(_make_request(path="/api/health", token="wrong")))


if __name__ == "__main__":
    unittest.main()
