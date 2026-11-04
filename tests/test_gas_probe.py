"""Offline synthetic tests for the bounded, redacted ENGIE gas probe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

import httpx


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"


def load_probe_module():
    # Import test-only vendor package without importing the HA integration
    # (__init__.py requires the whole Home Assistant runtime).
    for name, path in (
        ("_gasprobe_test", ROOT),
        ("_gasprobe_test.vendor", ROOT / "vendor"),
        ("_gasprobe_test.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
    ):
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(path)]
            sys.modules[name] = pkg
    name = "_gasprobe_test.gas_probe"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "gas_probe.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules[name]


class GasProbeTests(unittest.TestCase):
    def test_bounded_requests_no_credentials_and_same_query_parameters(self):
        module = load_probe_module()
        from _gasprobe_test.vendor.engie_particuliers.client import EngieClient

        requests = []
        def handler(request):
            requests.append(request)
            path = request.url.path
            if path.endswith("/getInfoUsers"):
                return httpx.Response(200, json={
                    "infosUsers": [{
                        "refBP": "SENSITIVE_BPARTNER",
                        "idPersonne": "SENSITIVE_PERSON",
                        "listeCC": [{
                            "numeroCC": "SENSITIVE_CONTRACT",
                            "adresse": {"cp": "SENSITIVE_POSTAL"},
                            "listeContrat": [{
                                "id": "SENSITIVE_METER",
                                "type": "GAZ",
                                "typeEnergie": "GAZ",
                                "libelle": "Test gas",
                            }]
                        }]
                    }]
                })
            if path.endswith("/recupererDonneesReleveV2"):
                return httpx.Response(200, json={
                    "contratGaz": {"compteurCommuniquant": False}
                })
            if path.endswith("/getInfoContrats"):
                return httpx.Response(200, json={"data": {"private": "SENSITIVE_VALUE"}})
            if path.endswith("/histoGazMois") or path.endswith("/histoGazJours"):
                if request.headers.get("Cache-Control") == "no-cache":
                    return httpx.Response(200, json={
                        "historiquesGaz": [{
                            "conso": 41.3,
                            "dateFin": "2025-01-31",
                            "contract": "SENSITIVE_VALUE"
                        }]
                    })
                return httpx.Response(204)
            raise AssertionError(f"Unexpected test endpoint: {path}")

        client = EngieClient("SENSITIVE_LOGIN", "SENSITIVE_PASSWORD")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "SENSITIVE_TOKEN"
        try:
            result = module.probe_gas_readonly(client)
        finally:
            client.close()

        self.assertEqual(result["gas_account_count"], 1)
        self.assertEqual(result["contract_v2_http_status"], 200)
        self.assertEqual(len(result["probes"]), 4)
        self.assertEqual([p["http_status"] for p in result["probes"]], [204, 200, 204, 200])
        self.assertEqual([p["cache_control_no_cache"] for p in result["probes"]], [False, True, False, True])
        self.assertEqual([p.get("history_row_count") for p in result["probes"]], [None, 1, None, 1])
        self.assertEqual(result["account_context"]["communicant"], False)
        # The request pairs must differ only in Cache-Control, not the
        # contract ID, date interval or selected meter parameters.
        for endpoint in ("histoGazMois", "histoGazJours"):
            matching = [r for r in requests if r.url.path.endswith("/" + endpoint)]
            self.assertEqual(len(matching), 2)
            self.assertEqual(matching[0].url, matching[1].url)
            self.assertIsNone(matching[0].headers.get("Cache-Control"))
            self.assertEqual(matching[1].headers.get("Cache-Control"), "no-cache")

        safe = json.dumps(result)
        for secret in (
            "SENSITIVE_BPARTNER", "SENSITIVE_PERSON", "SENSITIVE_CONTRACT",
            "SENSITIVE_POSTAL", "SENSITIVE_METER", "SENSITIVE_VALUE",
            "SENSITIVE_LOGIN", "SENSITIVE_PASSWORD", "SENSITIVE_TOKEN",
            "41.3",
        ):
            self.assertNotIn(secret, safe)

    def test_no_gas_account_returns_without_histories(self):
        module = load_probe_module()
        from _gasprobe_test.vendor.engie_particuliers.client import EngieClient
        paths = []

        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, json={"infosUsers": []})

        client = EngieClient("dummy", "dummy")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "fake"
        try:
            report = module.probe_gas_readonly(client)
        finally:
            client.close()
        self.assertEqual(report["gas_account_count"], 0)
        self.assertNotIn("probes", report)
        self.assertEqual(len(paths), 1)


if __name__ == "__main__":
    unittest.main()
