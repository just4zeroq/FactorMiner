"""Regression tests for the global auth dependency on websocket routes.

The bug: ``app = FastAPI(dependencies=[Depends(require_auth)])`` plus a
``require_auth(request: Request)`` signature. FastAPI's websocket routes call
``solve_dependencies(request=websocket)`` and only fill a Request-typed
parameter when the connection is an actual ``Request``. The argument was
therefore never injected, so *every* /ws/monitor handshake died with
``TypeError: require_auth() missing 1 required positional argument`` before
the function body (including the no-token shortcut) could run. HTTP routes
were unaffected, which is why the API looked healthy.

These tests drive the real ASGI app with a websocket scope instead of relying
on a running server or an optional websocket client library.
"""

import asyncio
import unittest
from unittest.mock import patch

import api.main as api_main


def _scope(path="/ws/monitor", scope_type="websocket", query=b"", headers=None):
    return {
        "type": scope_type,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws" if scope_type == "websocket" else "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 51111),
        "server": ("127.0.0.1", 8000),
        "subprotocols": [],
        "extensions": {},
    }


def _drive(token, scope):
    """Run one ASGI request; return (sent messages, exception or None)."""
    sent = []
    calls = {"n": 0}

    async def receive():
        calls["n"] += 1
        if calls["n"] == 1:
            if scope["type"] == "websocket":
                return {"type": "websocket.connect"}
            return {"type": "http.request", "body": b"", "more_body": False}
        if scope["type"] == "websocket":
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    error = None
    with patch.object(api_main, "API_TOKEN", token):
        try:
            asyncio.run(api_main.app(scope, receive, send))
        except Exception as exc:  # pragma: no cover - reported by the test
            error = exc
    return sent, error


def _accepted(sent):
    return any(msg.get("type") == "websocket.accept" for msg in sent)


def _status(sent):
    for msg in sent:
        if msg.get("type") in ("http.response.start", "websocket.http.response.start"):
            return msg.get("status")
    return None


class WebsocketAuthTests(unittest.TestCase):
    def setUp(self):
        self.connections_before = list(api_main.manager.active_connections)

    def tearDown(self):
        api_main.manager.active_connections[:] = self.connections_before

    # --- the original failure -------------------------------------------------

    def test_no_token_ws_handshake_succeeds(self):
        sent, error = _drive(None, _scope())
        self.assertIsNone(error, f"handshake raised {error!r}")
        self.assertTrue(_accepted(sent), "websocket.accept was never sent")

    def test_configured_token_ws_handshake_still_succeeds_with_credentials(self):
        # With the old Request signature this raised TypeError regardless of
        # whether a token was configured, because injection happens first.
        sent, error = _drive("s3cr3t", _scope(query=b"token=s3cr3t"))
        self.assertIsNone(error, f"handshake raised {error!r}")
        self.assertTrue(_accepted(sent))

    def test_http_is_unaffected_by_the_ws_fix(self):
        sent, error = _drive(None, _scope("/api/health", "http"))
        self.assertIsNone(error)
        self.assertEqual(_status(sent), 200)

    # --- auth semantics -------------------------------------------------------

    def test_ws_without_token_is_rejected_when_auth_is_configured(self):
        sent, error = _drive("s3cr3t", _scope())
        self.assertIsNone(error)
        self.assertFalse(_accepted(sent))
        self.assertEqual(_status(sent), 401)

    def test_ws_with_wrong_query_token_is_rejected(self):
        sent, _ = _drive("s3cr3t", _scope(query=b"token=nope"))
        self.assertFalse(_accepted(sent))
        self.assertEqual(_status(sent), 401)

    def test_ws_accepts_header_token_as_well(self):
        sent, _ = _drive("s3cr3t", _scope(headers=[(b"x-api-token", b"s3cr3t")]))
        self.assertTrue(_accepted(sent))

    def test_http_query_token_is_not_accepted(self):
        # The query fallback must stay websocket-only: otherwise tokens leak
        # into access logs and browser history.
        sent, _ = _drive("s3cr3t", _scope("/api/tasks", "http", query=b"token=s3cr3t"))
        self.assertEqual(_status(sent), 401)

    def test_http_header_token_is_accepted(self):
        sent, _ = _drive("s3cr3t", _scope("/api/tasks", "http",
                                          headers=[(b"x-api-token", b"s3cr3t")]))
        self.assertEqual(_status(sent), 200)

    def test_health_stays_public_when_auth_is_configured(self):
        sent, _ = _drive("s3cr3t", _scope("/api/health", "http"))
        self.assertEqual(_status(sent), 200)

    # --- structural guards ----------------------------------------------------

    def test_ws_route_keeps_the_global_auth_dependency(self):
        routes = [r for r in api_main.app.routes if getattr(r, "path", "") == "/ws/monitor"]
        self.assertEqual(len(routes), 1)
        dependant = getattr(routes[0], "dependant", None)
        names = [getattr(d.call, "__name__", "") for d in dependant.dependencies]
        self.assertIn("require_auth", names)

    def test_disconnect_removes_the_socket_from_the_broadcaster(self):
        sent, _ = _drive(None, _scope())
        self.assertTrue(_accepted(sent))
        # The scope's second receive is a disconnect, so the endpoint's finally
        # block must have cleaned the connection up before we return here.
        self.assertEqual(len(api_main.manager.active_connections),
                         len(self.connections_before))


if __name__ == "__main__":
    unittest.main()
