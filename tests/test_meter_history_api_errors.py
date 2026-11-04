"""Offline regression tests for fail-closed ENGIE meter history backfills.

An optional pricing endpoint may return HTTP 403, but the on-demand backfill
must never silently convert an HTTP error into a successful empty history.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest

import httpx


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"
for name, folder in (
    ("_meter_history_errors_test", ROOT),
    ("_meter_history_errors_test.vendor", ROOT / "vendor"),
    (
        "_meter_history_errors_test.vendor.engie_particuliers",
        ROOT / "vendor" / "engie_particuliers",
    ),
):
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(folder)]
        sys.modules[name] = package

from _meter_history_errors_test.vendor.engie_particuliers.client import EngieClient
from _meter_history_errors_test.vendor.engie_particuliers.exceptions import ApiError


SOURCE_ID = "meter_" + "a" * 20


class MeterHistoryApiErrorTests(unittest.TestCase):
    def make_client(self, status: int, payload: dict | None = None):
        requests = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if status == 204:
                return httpx.Response(204)
            return httpx.Response(status, json=payload or {})

        client = EngieClient("test@example.invalid", "FAKE_PASSWORD")
        client._http = httpx.Client(transport=httpx.MockTransport(handle))
        client._access_token = "FAKE_TOKEN"
        client._meter_source_routes[SOURCE_ID] = (
            "BP_FAKE", "CC_FAKE", "PERSON_FAKE", "75000", "GAZ"
        )
        self.addCleanup(client.close)
        return client, requests

    def fetch(self, client: EngieClient):
        return client.get_consumption_points_for_meter_source(
            SOURCE_ID, start_ms=1793491200000, end_ms=1793577600000
        )

    def test_failed_http_requests_propagate_for_backfill(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                client, requests = self.make_client(
                    status, {"message": "SYNTHETIC TEST ERROR"}
                )
                with self.assertRaises(ApiError) as caught:
                    self.fetch(client)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0].url.path.endswith("histoGazJours"))
                self.assertEqual(
                    requests[0].url.params.get("compteContrat"), "CC_FAKE"
                )

    def test_no_content_is_a_valid_empty_history(self):
        client, requests = self.make_client(204)
        self.assertEqual(self.fetch(client), [])
        self.assertEqual(len(requests), 1)

    def test_successful_daily_history_still_parses(self):
        client, _ = self.make_client(200, {
            "historiquesGaz": [{
                "valeurDate": "2026-11-01",
                "conso": 2.5,
                "consoEuroTTC": 0.42,
            }]
        })
        points = self.fetch(client)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].kwh, 2.5)

    def test_optional_polling_fallback_remains_compatible(self):
        client, _ = self.make_client(204)
        self.assertEqual(
            client._try_json(
                lambda: (_ for _ in ()).throw(ApiError(403, "optional"))
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
