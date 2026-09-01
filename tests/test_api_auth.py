from __future__ import annotations

import os
import asyncio
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import JSONResponse

from src.api import protect_write_apis


class ApiAuthTests(unittest.TestCase):
    @staticmethod
    def request(token: str | None = None, path: str = "/api/symbols") -> Request:
        headers = [] if token is None else [(b"x-api-key", token.encode())]
        return Request({
            "type": "http", "method": "POST", "path": path,
            "headers": headers, "query_string": b"", "scheme": "http",
            "server": ("test", 80), "client": ("test", 1),
        })

    @staticmethod
    async def next_response(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def test_write_endpoint_rejects_missing_token(self) -> None:
        with patch.dict(os.environ, {"KDJ_API_WRITE_TOKEN": "unit-test-token"}):
            response = asyncio.run(protect_write_apis(self.request(), self.next_response))
            self.assertEqual(response.status_code, 401)

    def test_write_endpoint_accepts_valid_token(self) -> None:
        with patch.dict(os.environ, {"KDJ_API_WRITE_TOKEN": "unit-test-token"}):
            response = asyncio.run(protect_write_apis(self.request("unit-test-token"), self.next_response))
            self.assertEqual(response.status_code, 200)

    def test_current_symbol_view_switch_does_not_require_token(self) -> None:
        with patch.dict(os.environ, {"KDJ_API_WRITE_TOKEN": "unit-test-token"}):
            response = asyncio.run(protect_write_apis(
                self.request(path="/api/current-symbol"), self.next_response
            ))
            self.assertEqual(response.status_code, 200)

    def test_token_verify_endpoint_remains_protected(self) -> None:
        with patch.dict(os.environ, {"KDJ_API_WRITE_TOKEN": "unit-test-token"}):
            rejected = asyncio.run(protect_write_apis(
                self.request(path="/api/auth/verify"), self.next_response
            ))
            accepted = asyncio.run(protect_write_apis(
                self.request("unit-test-token", path="/api/auth/verify"), self.next_response
            ))
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)


if __name__ == "__main__":
    unittest.main()
