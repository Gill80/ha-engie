"""Offline regression for the vendored PCE-selected gas data source."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import types
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"
for name, folder in (
    ("_gas_pce_test", ROOT),
    ("_gas_pce_test.vendor", ROOT / "vendor"),
    ("_gas_pce_test.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
):
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(folder)]
        sys.modules[name] = package

from _gas_pce_test.vendor.engie_particuliers.client import EngieClient


class GasPceSourceTests(unittest.TestCase):
    def test_meter_in_electricity_account_powers_gas_snapshot(self):
        pce = "77777777777777"
        calls = []

        def handle(request):
            path = request.url.path
            cc = request.url.params.get("compteContrat", "")
            calls.append((path, cc))
            if path.endswith("getInfoUsers"):
                return httpx.Response(200, json={"infosUsers": [{
                    "refBP": "FAKE_BP", "idPersonne": "FAKE_PERSON",
                    "listeCC": [
                        {"numeroCC": "ACTIVE", "adresse": {"cp": "75000"},
                         "listeContrat": [{"id": "E_ID", "typeEnergie": "ELEC"}]},
                        {"numeroCC": "OLD", "adresse": {"cp": "75000"},
                         "listeContrat": [{"id": "G_ID", "typeEnergie": "GAZ"}]},
                    ],
                }]})
            if path.endswith("recupererDonneesReleveV2"):
                return httpx.Response(200, json={"contratGaz": {
                    "idPDL": pce if cc == "ACTIVE" else "11111111111111",
                    "compteurCommuniquant": True,
                }})
            if path.endswith("histoGazMois"):
                if cc == "ACTIVE":
                    return httpx.Response(200, json={"historiquesGaz": [{
                        "conso": 100, "consoEuroTTC": 12.5,
                        "abonnementTTC": 6.0, "dateFin": "2026-10-31"
                    }]})
                return httpx.Response(204)
            if path.endswith("histoGazJours"):
                if cc == "ACTIVE":
                    return httpx.Response(200, json={"historiquesGaz": [{
                        "conso": 10, "consoEuroTTC": 1.25,
                        "valeurDate": "2026-10-30"
                    }]})
                return httpx.Response(204)
            if path.endswith("histoElecMois") or path.endswith("histoElecJours"):
                return httpx.Response(200, json={"historiquesElec": []})
            if path.endswith("ecoGestesDynamiques"):
                return httpx.Response(200, json={"prixKwh": 0.19})
            if path.endswith("estimationConsoGazMois"):
                return httpx.Response(403, json={"status": "fake"})
            if path.endswith("listeFacture") or path.endswith("soldeApayerV2"):
                self.assertIn("Cel_Cc=" + cc, request.headers.get("Cookie", ""))
                return httpx.Response(200, json={})
            return httpx.Response(204)

        client = EngieClient("fake@example.invalid", "FAKE_PASSWORD")
        client._http = httpx.Client(transport=httpx.MockTransport(handle))
        client._access_token = "FAKE_TOKEN"
        try:
            client.set_gas_pce_sha256(hashlib.sha256(pce.encode()).hexdigest())
            snapshot = client.get_snapshot(daily_days=14)
            gas = next(c for c in snapshot.contracts if c.price.energy == "GAZ")
            self.assertEqual(gas.price.contract_id, "G_ID")
            self.assertEqual(gas.price.source, "histoGazMois")
            self.assertEqual(gas.price.price_kwh_ttc, 0.125)
            self.assertEqual(len(gas.recent_days), 1)
            self.assertEqual(gas.recent_days[0].kwh, 10)
            points = client.get_consumption_points_for_contract("G_ID", days=14)
            self.assertEqual(len(points), 1)
            # Legacy gas route stays linked to the selected active PCE.
            # Independent meter-source discovery also examines the old
            # account, but does not merge its data with the active meter.
            self.assertTrue(any(
                path.endswith("histoGazJours") and cc == "OLD"
                for path, cc in calls
            ))
            older = next(
                source for source in snapshot.meter_sources
                if source.energy == "GAZ" and source.account_suffix == "OLD"
            )
            self.assertEqual(len(older.recent_days), 0)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
