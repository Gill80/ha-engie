"""Offline tests for local ENGIE complete-response dump, with synthetic data.

No network, no real credentials and no meter IDs are used. The response
contains fake private values that must never appear in diagnostic output.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

import httpx


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"


def _load():
    for name, folder in (
        ("_dump_test", ROOT),
        ("_dump_test.vendor", ROOT / "vendor"),
        ("_dump_test.vendor.engie_particuliers", ROOT / "vendor" / "engie_particuliers"),
    ):
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(folder)]
            sys.modules[name] = pkg
    key = "_dump_test.private_api_dump"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, ROOT / "private_api_dump.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules[key]


class PrivateEngieDumpTests(unittest.TestCase):
    def test_local_private_dump_contains_full_bodies_and_redacts_auth_fields(self):
        m = _load()
        from _dump_test.vendor.engie_particuliers.client import EngieClient

        target_pce = "77777777777777"  # invented reference
        observed = []
        def handler(request):
            path = request.url.path
            observed.append(path)
            if path.endswith("/getInfoUsers"):
                return httpx.Response(200, json={
                    "infosUsers": [
                        {
                            "refBP": "FAKE_BP",
                            "idPersonne": "FAKE_PERSON",
                            "listeCC": [
                                {"numeroCC": "OLD_ACCOUNT",
                                 "adresse": {"cp": "75000"},
                                 "listeContrat": [{"typeEnergie": "ELEC"}]},
                                {"numeroCC": "LIVE_GAS",
                                 "adresse": {"cp": "75000"},
                                 "listeContrat": [{"typeEnergie": "GAZ"}]},
                            ],
                            "access_token": "SHOULD_NEVER_APPEAR",
                        }
                    ]
                })
            if path.endswith("/getInfoContrats"):
                if request.url.params.get("compteContrat") == "LIVE_GAS":
                    return httpx.Response(200, json={
                        "details": {"meter": {"pce": target_pce,
                                              "site_name": "PRIVATE_HOUSE_LABEL"}},
                        "sessionToken": "HIDDEN_SESSION_TOKEN"
                    })
                return httpx.Response(200, json={
                    "details": {"meter": {"pce": "11111111111111"}}
                })
            if path.endswith("/recupererDonneesReleveV2"):
                return httpx.Response(200, json={
                    "contratGaz": {"compteurCommuniquant": True}
                })
            if path.endswith("/estimationConsoGazMois"):
                return httpx.Response(403, json={"status": "PRIVATE_STATUS"})
            if path.endswith("/histoGazMois") or path.endswith("/histoGazJours"):
                return httpx.Response(204)
            raise AssertionError("Unknown synthetic endpoint " + path)

        client = EngieClient("PRIVATE_EMAIL", "PRIVATE_PASSWORD")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._access_token = "PRIVATE_BEARER"
        client._http.cookies.set("Cel_Cc", "BEFORE_TEST",
                                 domain="particuliers.engie.fr", path="/")
        try:
            with tempfile.TemporaryDirectory() as directory:
                report = m.dump_engie_api_private(
                    client, directory, hashlib.sha256(target_pce.encode()).hexdigest()
                )
                fullpath = Path(report["local_private_path"])
                self.assertTrue(fullpath.is_file())
                self.assertEqual(os.stat(fullpath).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(fullpath.parent).st_mode & 0o777, 0o700)
                dump_text = fullpath.read_text(encoding="utf-8")
                document = json.loads(dump_text)
                self.assertEqual(report["responses_saved"], 10)
                self.assertEqual(document["response_count"], 10)
                self.assertEqual(len(observed), 10)
                self.assertTrue(report["pce_found_anywhere"])
                self.assertEqual(
                    [x["endpoint"] for x in document["responses"]].count("histoGazJours"), 2
                )
                self.assertIn(target_pce, dump_text)
                self.assertIn("PRIVATE_HOUSE_LABEL", dump_text)
                self.assertNotIn("SHOULD_NEVER_APPEAR", dump_text)
                self.assertNotIn("HIDDEN_SESSION_TOKEN", dump_text)
                self.assertNotIn("PRIVATE_BEARER", dump_text)
                visible = json.dumps(report)
                for secret in (
                    target_pce, "FAKE_BP", "FAKE_PERSON",
                    "OLD_ACCOUNT", "LIVE_GAS", "PRIVATE_HOUSE_LABEL",
                    "PRIVATE_EMAIL", "PRIVATE_PASSWORD", "PRIVATE_BEARER",
                    "PRIVATE_STATUS", "BEFORE_TEST", "75000",
                ):
                    self.assertNotIn(secret, visible)
                self.assertEqual(client._http.cookies.get("Cel_Cc"), "BEFORE_TEST")
        finally:
            client.close()

    def test_cannot_make_a_dump_with_invalid_digest(self):
        m = _load()
        with self.assertRaises(ValueError):
            m.dump_engie_api_private(object(), "/nonexistent", "not-a-valid-sha")

    def test_field_names_are_preserved_but_dynamic_keys_hidden(self):
        m = _load()
        schema = m._structure_paths({
            "gaz": {"20111111111111": {"consoEuroTTC": 28.0}},
            "historiquesGaz": [{"conso": 10.5, "prevConso": True}]
        })
        paths = json.dumps(schema)
        self.assertIn("consoEuroTTC", paths)
        self.assertIn("prevConso", paths)
        self.assertNotIn("20111111111111", paths)


if __name__ == "__main__":
    unittest.main()
