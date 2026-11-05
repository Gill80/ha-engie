"""Offline tests for independent ENGIE account / energy devices.

Every account reference, PCE and tariff in this file is synthetic.
"""

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


def _load_client():
    for name, path in (
        ("_groups_test", ROOT),
        ("_groups_test.vendor", ROOT / "vendor"),
        ("_groups_test.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(path)]
            sys.modules[name] = package
    key = "_groups_test.vendor.engie_particuliers.client"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            key, ROOT / "vendor" / "engie_particuliers" / "client.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules[key]


class AccountEnergyGroupTests(unittest.TestCase):
    def _snapshot(
        self,
        *,
        active_gas: bool = True,
        selected_ids: list[str] | None = None,
    ):
        mod = _load_client()
        active_pce = "7" * 14
        retired_pce = "1" * 14
        electric_pdl = "8" * 14

        def handler(req):
            p = req.url.path
            account = req.url.params.get("compteContrat")
            if p.endswith("/getInfoUsers"):
                return httpx.Response(200, json={
                    "infosUsers": [{
                        "refBP": "TEST_BP", "idPersonne": "TEST_PERSON",
                        "listeCC": [
                            {"numeroCC": "NEW_CC_1234",
                             "adresse": {"cp": "75000"},
                             "listeContrat": [
                                 {"id": "elec_contract", "typeEnergie": "ELEC",
                                  "libelle": "Électricité test"}
                             ]},
                            {"numeroCC": "OLD_CC_5678",
                             "adresse": {"cp": "75000"},
                             "listeContrat": [
                                 {"id": "gaz_contract", "typeEnergie": "GAZ",
                                  "libelle": "Gaz ancien"}
                             ]},
                        ],
                    }]
                })
            if p.endswith("/recupererDonneesReleveV2"):
                return httpx.Response(200, json={
                    "contratGaz": {"idPDL": (
                        active_pce if account == "NEW_CC_1234" else retired_pce
                    ), "compteurCommuniquant": True},
                    "contratElec": (
                        {"idPDL": electric_pdl, "compteurCommuniquant": True}
                        if account == "NEW_CC_1234" else {}
                    ),
                })
            if p.endswith("/ecoGestesDynamiques"):
                return httpx.Response(200, json={"prixKwh": 0.19})
            if p.endswith("/estimationConsoGazMois"):
                return httpx.Response(403, json={"status": "not_authorized"})
            if p.endswith("/histoGazMois"):
                if account != "NEW_CC_1234" or not active_gas:
                    return httpx.Response(204)
                return httpx.Response(200, json={
                    "historiquesGaz": [{
                        "conso": 100.0, "consoEuroTTC": 12.5,
                        "abonnementTTC": 3.0,
                        "dateDebut": "2026-10-01", "dateFin": "2026-10-31",
                    }]
                })
            if p.endswith("/histoGazJours"):
                if account != "NEW_CC_1234" or not active_gas:
                    return httpx.Response(204)
                return httpx.Response(200, json={
                    "historiquesGaz": [{
                        "conso": 4.0, "consoEuroTTC": 0.5,
                        "valeurDate": "2026-10-31",
                    }]
                })
            if p.endswith("/histoElecMois"):
                return httpx.Response(200, json={
                    "historiquesElec": [
                        {"conso": 100, "consoEuroTTC": 20,
                         "dateDebut": "2026-10-01", "dateFin": "2026-10-31"}
                    ]
                })
            if p.endswith("/histoElecJours"):
                return httpx.Response(200, json={
                    "historiquesElec": [
                        {"conso": 5, "consoEuroTTC": 1,
                         "valeurDate": "2026-10-31"}
                    ]
                })
            return httpx.Response(200, json={})

        client = mod.EngieClient("synthetic@invalid.test", "fictional-password")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "fictional-token"
        client.set_gas_pce_sha256(hashlib.sha256(active_pce.encode()).hexdigest())
        if selected_ids is not None:
            client.configure_meter_sources("selected", selected_ids)
        try:
            snapshot = client.get_snapshot(daily_days=31)
        finally:
            client.close()
        return snapshot

    def test_three_independent_source_groups_with_active_and_old_gas(self):
        snapshot = self._snapshot()
        groups = snapshot.meter_sources
        self.assertEqual(len(groups), 3)
        self.assertEqual(
            [(s.account_suffix, s.energy) for s in groups],
            [("1234", "ELEC"), ("1234", "GAZ"), ("5678", "GAZ")]
        )
        active = groups[1]
        old = groups[2]
        self.assertTrue(active.matched_selected_pce)
        self.assertFalse(old.matched_selected_pce)
        self.assertEqual(active.price.source, "histoGazMois")
        self.assertAlmostEqual(active.price.price_kwh_ttc, 0.125)
        self.assertIsNone(old.price.price_kwh_ttc)
        self.assertEqual(len(active.recent_days), 1)
        self.assertEqual(len(old.recent_days), 0)
        self.assertIn("Gaz · CC …1234 · PCE …7777", active.display_name)
        self.assertIn("Gaz · CC …5678 · PCE …1111", old.display_name)
        # The original commercial-contract identities are preserved.
        self.assertEqual(
            [x.price.contract_id for x in snapshot.contracts],
            ["elec_contract", "gaz_contract"]
        )
        source_ids = [s.source_id for s in groups]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        serialized = json.dumps(snapshot.to_dict())
        for sensitive in ("77777777777777", "11111111111111",
                          "88888888888888", "TEST_BP", "TEST_PERSON"):
            self.assertNotIn(sensitive, serialized)

    def test_discovery_is_stable_even_when_gas_is_not_consumed(self):
        first = self._snapshot(active_gas=False)
        second = self._snapshot(active_gas=False)
        self.assertEqual(
            [s.source_id for s in first.meter_sources],
            [s.source_id for s in second.meter_sources],
        )
        self.assertEqual(len(first.meter_sources), 3)
        self.assertIsNone(first.meter_sources[1].price.price_kwh_ttc)

    def test_selected_sources_keep_inventory_but_skip_other_meter_readings(self):
        active_id = "meter_" + hashlib.sha256(
            ("TEST_BP|NEW_CC_1234|GAZ|" + "7" * 14).encode()
        ).hexdigest()[:20]
        snapshot = self._snapshot(selected_ids=[active_id])
        groups = snapshot.meter_sources
        self.assertEqual(len(groups), 3)
        self.assertEqual(len(groups[1].recent_days), 1)
        self.assertEqual(len(groups[0].recent_days), 0)
        self.assertEqual(len(groups[2].recent_days), 0)
        self.assertEqual(
            groups[0].price.notes,
            "Meter source not selected for monitoring",
        )

    def test_no_automatic_source_statistic_import(self):
        source = (ROOT / "statistics.py").read_text()
        self.assertIn("for item in snapshot.contracts:", source)
        self.assertNotIn("for item in snapshot.meter_sources:", source)
        source = (ROOT / "sensor.py").read_text()
        self.assertIn('if spec.source == "energy_total":', source)
        self.assertIn("EngieMeterSourceSensor", source)


if __name__ == "__main__":
    unittest.main()
