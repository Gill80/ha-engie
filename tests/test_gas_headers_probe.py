"""Synthetic offline tests for ENGIE 10.28/10.31 gas User-Agent comparison."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

import httpx


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"


def _load():
    for name, folder in (
        ("_headers_test", ROOT),
        ("_headers_test.vendor", ROOT / "vendor"),
        ("_headers_test.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
    ):
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(folder)]
            sys.modules[name] = pkg
    name = "_headers_test.gas_headers_probe"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "gas_headers_probe.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules[name]


class GasMobileHeaderProbeTests(unittest.TestCase):
    def test_six_requests_change_only_user_agent_and_never_leak_personal_data(self):
        module = _load()
        from _headers_test.vendor.engie_particuliers.client import EngieClient
        observed = []

        def handler(request):
            path = request.url.path
            if path.endswith("/getInfoUsers"):
                return httpx.Response(
                    200,
                    json={"infosUsers": [{
                        "refBP": "REDACT_BP_0001",
                        "idPersonne": "REDACT_PERSON_0002",
                        "listeCC": [{
                            "numeroCC": "REDACT_CC_0003",
                            "adresse": {"cp": "REDACT_POSTAL_0004"},
                            "listeContrat": [{"typeEnergie": "GAZ", "id": "REDACT_ID"}],
                        }]
                    }]},
                    headers={"Set-Cookie": "secureAPI_SEC_NUM_CC=dummy-cookie; Path=/; Secure"},
                )
            if path.endswith("/recupererDonneesReleveV2"):
                return httpx.Response(200, json={
                    "contratGaz": {"compteurCommuniquant": True}
                })
            if path.endswith(("histoGazJours", "histoGazMois", "estimationConsoGazMois")):
                observed.append(request)
                if request.headers["User-Agent"].startswith("Mozilla/10.28.0"):
                    if path.endswith("estimationConsoGazMois"):
                        return httpx.Response(403, json={"status": "denied"})
                    return httpx.Response(204)
                if path.endswith("estimationConsoGazMois"):
                    return httpx.Response(200, json={"prixKwh": 0.075})
                return httpx.Response(200, json={
                    "historiquesGaz": [
                        {"conso": 42.64, "private": "REDACT_CUSTOMER_AMOUNT"}
                    ],
                })
            raise AssertionError("Unexpected URL in mocked test")

        client = EngieClient("REDACT_USER", "REDACT_PASSWORD")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "REDACT_ACCESS_TOKEN"
        try:
            result = module.probe_gas_mobile_headers_readonly(client)
        finally:
            client.close()

        self.assertEqual(len(observed), 6)
        self.assertEqual(result["gas_account_count"], 1)
        self.assertEqual(result["probe_version"], 2)
        self.assertEqual(
            [x["http_status"] for x in result["responses"]],
            [403, 200, 204, 200, 204, 200],
        )
        self.assertTrue(result["cookie_presence"]["access_token"])
        self.assertTrue(result["cookie_presence"]["Cel_Cc"])
        self.assertTrue(result["cookie_presence"]["Cel_Bp"])
        self.assertTrue(result["cookie_presence"]["idPersonne"])
        self.assertTrue(result["cookie_presence"]["secureAPI_SEC_NUM_CC"])
        for index in range(0, 6, 2):
            a, b = observed[index:index+2]
            self.assertEqual(a.url, b.url)
            self.assertTrue(a.headers["User-Agent"].startswith("Mozilla/10.28.0"))
            self.assertTrue(b.headers["User-Agent"].startswith("Mozilla/10.31.0"))
            self.assertEqual(a.headers.get("Cookie"), b.headers.get("Cookie"))
        report = json.dumps(result)
        for value in (
            "REDACT_BP_0001", "REDACT_PERSON_0002", "REDACT_CC_0003",
            "REDACT_POSTAL_0004", "REDACT_ID",
            "REDACT_ACCESS_TOKEN", "REDACT_PASSWORD", "REDACT_USER",
            "REDACT_CUSTOMER_AMOUNT", "42.64", "dummy-cookie",
        ):
            self.assertNotIn(value, report)

    def test_no_gas_account_makes_no_gas_calls(self):
        module = _load()
        from _headers_test.vendor.engie_particuliers.client import EngieClient
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"infosUsers": []})

        client = EngieClient("demo", "secret")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "fictional-token"
        try:
            result = module.probe_gas_mobile_headers_readonly(client)
        finally:
            client.close()
        self.assertEqual(result["gas_account_count"], 0)
        self.assertNotIn("responses", result)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
