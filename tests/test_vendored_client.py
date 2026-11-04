"""Prove the staged ENGIE integration imports its own forked library."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"


class VendoredClientIntegrationTests(unittest.TestCase):
    def test_vendored_client_exposes_gas_diagnostics(self):
        content = (ROOT / "vendor" / "engie_particuliers" / "client.py").read_text()
        self.assertIn("def gas_api_diagnostics(self)", content)
        self.assertIn("def _record_gas_response(", content)

    def test_integration_uses_vendored_client(self):
        for name in ("api.py", "config_flow.py", "coordinator.py",
                     "sensor.py", "statistics.py"):
            source = (ROOT / name).read_text()
            ast.parse(source)
            self.assertNotIn("from engie_particuliers.", source, name)
            self.assertIn("vendor.engie_particuliers", source, name)

    def test_manifest_has_no_pypi_client_dependency(self):
        import json
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["requirements"], ["httpx>=0.27.0"])


if __name__ == "__main__":
    unittest.main()
