import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
import web_demo_server


class AdminProxyTests(unittest.TestCase):
    def test_proxy_forwards_key_in_header_and_preserves_status(self):
        upstream = AsyncMock()
        upstream.get.return_value = httpx.Response(401, json={"detail": "관리자 접근 키를 확인해주세요."})
        manager = AsyncMock()
        manager.__aenter__.return_value = upstream
        with patch("web_demo_server.httpx.AsyncClient", return_value=manager):
            with TestClient(web_demo_server.app) as client:
                response = client.get("/admin/api/runs?limit=10", headers={"X-Admin-Token": "test-token"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(upstream.get.call_args.kwargs["headers"]["X-Admin-Token"], "test-token")
        self.assertEqual(upstream.get.call_args.kwargs["params"], {"limit": 10})
        self.assertNotIn("test-token", upstream.get.call_args.args[0])

    def test_unavailable_backend_returns_friendly_error(self):
        upstream = AsyncMock()
        upstream.get.side_effect = httpx.ConnectError("private internal information")
        manager = AsyncMock()
        manager.__aenter__.return_value = upstream
        with patch("web_demo_server.httpx.AsyncClient", return_value=manager):
            with TestClient(web_demo_server.app) as client:
                response = client.get("/admin/api/runs")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private internal information", response.text)


if __name__ == "__main__":
    unittest.main()
