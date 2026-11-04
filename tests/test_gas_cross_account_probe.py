"""Offline regression: active PCE can be on a nominally electricity account."""

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
    for name, folder in (
        ("_cross_test", ROOT),
        ("_cross_test.vendor", ROOT / "vendor"),
        ("_cross_test.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
    ):
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(folder)]
            sys.modules[name] = pkg
    for subpath in ("private_api_dump.py", "gas_cross_account_probe.py"):
        name = "_cross_test." + subpath[:-3]
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, ROOT / subpath)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            assert spec.loader is not None
            spec.loader.exec_module(module)
    return sys.modules["_cross_test.gas_cross_account_probe"]


class CrossAccountTests(unittest.TestCase):
    def test_other_account_has_correct_pce_and_gas_history(self):
        module = _load()
        from _cross_test.vendor.engie_particuliers.client import EngieClient

        pce = "77777777777777"  # synthetic only
        queries = []
        def handler(req):
            path = req.url.path
            if path.endswith("/getInfoUsers"):
                return httpx.Response(200, json={"infosUsers": [{
                    "refBP": "TEST_BP",
                    "idPersonne": "TEST_PERSON",
                    "listeCC": [
                        {"numeroCC": "ACTIVE_PCE_ACCOUNT", "adresse": {"cp": "75000"},
                         "listeContrat": [{"typeEnergie": "ELEC"}]},
                        {"numeroCC": "OLD_GAS_ACCOUNT", "adresse": {"cp": "75000"},
                         "listeContrat": [{"typeEnergie": "GAZ"}]},
                    ],
                }]})
            account = req.url.params.get("compteContrat")
            if path.endswith("/recupererDonneesReleveV2"):
                return httpx.Response(200, json={"contratGaz": {
                    "idPDL": pce if account == "ACTIVE_PCE_ACCOUNT" else "11111111111111"
                }})
            queries.append((path, account, req.headers.get("Cookie")))
            if account == "OLD_GAS_ACCOUNT":
                return httpx.Response(204)
            if path.endswith("/estimationConsoGazMois"):
                return httpx.Response(200, json={"prixKwh": 0.11})
            return httpx.Response(200, json={
                "historiquesGaz": [{"conso": 42.0, "consoEuroTTC": 5.0,
                                    "address": "SECRET_PRIVATE_LOCATION"}],
                "previsionGaz": [],
            })

        client = EngieClient("TEST_USER", "TEST_PASSWORD")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "TEST_ACCESS_TOKEN"
        client._http.cookies.set(
            "Cel_Cc", "ORIGINAL_CC", domain="particuliers.engie.fr", path="/"
        )
        try:
            result = module.probe_gas_across_accounts(
                client, hashlib.sha256(pce.encode()).hexdigest()
            )
            self.assertEqual(result["account_count"], 2)
            self.assertEqual(len(queries), 10)
            first, second = result["comparisons"]
            self.assertTrue(first["meter_pce_match"])
            self.assertFalse(second["meter_pce_match"])
            self.assertEqual(first["listed_gas_contracts"], 0)
            self.assertEqual(second["listed_gas_contracts"], 1)
            self.assertTrue(all(
                r["http_status"] == 200 for r in first["requests"]
            ))
            self.assertTrue(all(
                r["http_status"] == 204 for r in second["requests"]
            ))
            self.assertEqual(first["requests"][1]["historiquesGaz_count"], 1)
            self.assertIn("historiquesGaz", first["requests"][1]["top_level_field_names"])
            self.assertEqual(
                client._http.cookies.get("Cel_Cc"), "ORIGINAL_CC"
            )
            output = json.dumps(result)
            for private in (
                pce, "11111111111111", "TEST_BP", "TEST_PERSON",
                "ACTIVE_PCE_ACCOUNT", "OLD_GAS_ACCOUNT", "TEST_USER",
                "TEST_PASSWORD", "TEST_ACCESS_TOKEN", "SECRET_PRIVATE_LOCATION",
                "75000", "42.0",
            ):
                self.assertNotIn(private, output)
        finally:
            client.close()

    def test_detected_gas_meter_remains_separate_from_other_contract(self):
        _load()
        from _cross_test.vendor.engie_particuliers.client import EngieClient
        from _cross_test.vendor.engie_particuliers.models import (
            CompteContrat, EnergyContract,
        )

        client = EngieClient()
        client.select_account = lambda *args: None
        client.get_releve = lambda *args: {
            "contratGaz": {"idPDL": "7" * 14, "compteurCommuniquant": True}
        }
        client.get_eco_gestes = lambda *args: {"prixKwh": 0.19}
        client.get_estimation_elec = lambda *args: {}
        client.get_estimation_gaz = lambda *args: {}
        client._history_with_fallback = lambda energy, **kwargs: (
            {"historiquesGaz": [
                {"conso": 100, "consoEuroTTC": 12.5,
                 "dateDebut": "2026-10-01", "dateFin": "2026-10-31"}
            ]} if energy == "GAZ" else {}
        )
        cc = CompteContrat(
            numero_cc="fictional-account",
            contrats=[EnergyContract(id="electric", type_energie="ELEC")]
        )
        prices = client._prices_for_compte("fictional-bp", "fictional-person", cc)
        self.assertEqual(len(prices), 2)
        self.assertEqual(prices[0].energy, "ELEC")
        self.assertEqual(prices[1].energy, "GAZ")
        self.assertAlmostEqual(prices[1].price_kwh_ttc, 0.125)
        self.assertTrue(prices[1].contract_id.startswith("detected_gas_"))
        self.assertIn(prices[1].contract_id, client._discovered_gas_routes)

    def test_no_discovery_without_gas_history(self):
        _load()
        from _cross_test.vendor.engie_particuliers.client import EngieClient
        from _cross_test.vendor.engie_particuliers.models import CompteContrat, EnergyContract

        client = EngieClient()
        client.select_account = lambda *args: None
        client.get_releve = lambda *args: {"contratGaz": {"idPDL": "7" * 14}}
        client.get_eco_gestes = lambda *args: {}
        client.get_estimation_elec = lambda *args: {}
        client.get_estimation_gaz = lambda *args: {}
        client._history_with_fallback = lambda *args, **kwargs: {}
        cc = CompteContrat(
            numero_cc="fictional",
            contrats=[EnergyContract(id="electric", type_energie="ELEC")]
        )
        self.assertEqual(len(client._prices_for_compte("bp", "person", cc)), 1)

    def test_invalid_hash_fails_before_request(self):
        with self.assertRaises(ValueError):
            _load().probe_gas_across_accounts(object(), "not_a_digest")


if __name__ == "__main__":
    unittest.main()
