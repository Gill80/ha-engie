"""Synthetic tests: identify gas PCE without exposing any real account data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

import httpx


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"


def _load():
    for name, directory in (
        ("_meter_test", ROOT),
        ("_meter_test.vendor", ROOT / "vendor"),
        ("_meter_test.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
    ):
        if name not in sys.modules:
            p = types.ModuleType(name)
            p.__path__ = [str(directory)]
            sys.modules[name] = p
    module_name = "_meter_test.gas_meter_audit"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            module_name, ROOT / "gas_meter_audit.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules[module_name]


class GasMeterSelectionTests(unittest.TestCase):
    def test_matches_second_account_and_preserves_cookies(self):
        module = _load()
        from _meter_test.vendor.engie_particuliers.client import EngieClient

        # All identifiers and readings are invented for this mock.
        target = "77777777777777"
        older = "11111111111111"
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/getInfoUsers"):
                return httpx.Response(200, json={
                    "infosUsers": [{
                        "refBP": "TEST_BP",
                        "idPersonne": "TEST_PERSON",
                        "listeCC": [
                            {"numeroCC": "OLDER_CC", "listeContrat": [
                                {"id": "fake_old", "typeEnergie": "GAZ"}
                            ]},
                            {"numeroCC": "ACTIVE_CC", "listeContrat": [
                                {"id": "fake_active", "typeEnergie": "GAZ"}
                            ]},
                        ],
                    }]
                })
            selected = request.url.params.get("compteContrat")
            if request.url.path.endswith("/recupererDonneesReleveV2"):
                return httpx.Response(200, json={
                    "contratGaz": {"numPCE": target if selected == "ACTIVE_CC" else older}
                })
            if request.url.path.endswith("/getInfoContrats"):
                assert "/v2/" in request.url.path
                return httpx.Response(200, json={
                    "contratGaz": {"idPDLGaz": target if selected == "ACTIVE_CC" else older}
                })
            raise AssertionError("unexpected test endpoint")

        client = EngieClient("TEST_EMAIL", "TEST_PASSWORD")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "TEST_ACCESS_TOKEN"
        client._http.cookies.set("Cel_Cc", "ORIGINAL_CC", domain="particuliers.engie.fr", path="/")
        original = client._http.cookies.get("Cel_Cc")
        try:
            result = module.audit_gas_meter_selection(
                client, hashlib.sha256(target.encode()).hexdigest()
            )
            self.assertEqual(result["account_counts"]["accounts_with_gas_contract"], 2)
            self.assertFalse(result["account_checks"][0]["releve_v2"]["matches_supplied_gas_pce"])
            self.assertTrue(result["account_checks"][1]["releve_v2"]["matches_supplied_gas_pce"])
            self.assertTrue(result["account_checks"][1]["contract_details_v2"]["matches_supplied_gas_pce"])
            self.assertEqual(client._http.cookies.get("Cel_Cc"), original)
            self.assertEqual(result["accounts_checked"], 2)
            self.assertEqual(len(calls), 5)
            # No PCE, password, cookie or other private value in the report.
            serialized = json.dumps(result)
            for forbidden in (
                target, older, "TEST_BP", "TEST_PERSON",
                "OLDER_CC", "ACTIVE_CC", "ORIGINAL_CC",
                "TEST_EMAIL", "TEST_PASSWORD", "TEST_ACCESS_TOKEN"
            ):
                self.assertNotIn(forbidden, serialized)
        finally:
            client.close()

    def test_refusal_or_missing_meter_fields_never_fabricates_a_match(self):
        module = _load()
        from _meter_test.vendor.engie_particuliers.client import EngieClient

        def handler(request):
            if request.url.path.endswith("/getInfoUsers"):
                return httpx.Response(200, json={
                    "infosUsers": [{"refBP": "bp", "listeCC": [
                        {"numeroCC": "cc", "listeContrat": [
                            {"typeEnergie": "GAZ"}
                        ]}
                    ]}]
                })
            return httpx.Response(403, json={"message": "private detail not shared"})

        client = EngieClient()
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "synthetic-token"
        try:
            report = module.audit_gas_meter_selection(
                client, hashlib.sha256(b"77777777777777").hexdigest()
            )
            self.assertEqual(report["account_counts"]["accounts_with_gas_contract"], 1)
            self.assertEqual(report["account_checks"][0]["releve_v2"]["http_status"], 403)
            self.assertFalse(report["user_listing"]["matches_supplied_gas_pce"])
            self.assertNotIn("private detail", json.dumps(report))
        finally:
            client.close()

    def test_invalid_digest_rejected_before_any_api_call(self):
        with self.assertRaises(ValueError):
            _load().audit_gas_meter_selection(object(), "not a hash")


if __name__ == "__main__":
    unittest.main()
