"""Verify retired ENGIE device removal is narrowly guarded.

Only inactive, entity-less legacy contract devices can be deleted.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "engie"


class RetiredDeviceTests(unittest.TestCase):
    def test_device_deletion_requires_legacy_mode_disabled_and_no_entities(self):
        source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        matches = [
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_remove_config_entry_device"
        ]
        self.assertEqual(len(matches), 1)
        body = ast.get_source_segment(source, matches[0])
        self.assertIn("legacy_contract_sensors", body)
        self.assertIn('identity.startswith("contract_")', body)
        self.assertIn("er.async_entries_for_device", body)
        self.assertNotIn("async_remove_device(", body)
        self.assertNotIn("meter_source_", body)

    def test_groups_are_separate_from_legacy_devices(self):
        text = (ROOT / "entity.py").read_text(encoding="utf-8")
        self.assertIn('f"meter_source_{source_id}"', text)
        self.assertIn('f"contract_{contract_id}"', text)


if __name__ == "__main__":
    unittest.main()
