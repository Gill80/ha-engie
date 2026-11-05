"""Offline regression tests for the external statistics metadata contract.

Only the _metadata function is loaded from AST: importing the entire
Home Assistant integration would require a running HA environment.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


def load_metadata_function():
    path = Path(__file__).resolve().parents[1] / "custom_components" / "engie" / "statistics.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_metadata"
    )
    isolated = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    namespace = {
        "DOMAIN": "engie",
        "STREAM_COST": "cost",
        "STREAM_ENERGY": "energy",
        "UnitOfEnergy": SimpleNamespace(KILO_WATT_HOUR="kWh"),
        "EnergyConverter": SimpleNamespace(UNIT_CLASS="energy"),
        "StatisticMetaData": dict,
        "statistic_id": lambda contract_id, stream: f"engie:{contract_id}_{stream}",
    }
    exec(compile(isolated, str(path), "exec"), namespace)
    return namespace["_metadata"]


class ExternalStatisticMetadataTests(unittest.TestCase):
    def test_cost_explicitly_provides_no_unit_class(self):
        result = load_metadata_function()("demo-contract", "cost", "Test")
        self.assertIn("unit_class", result)
        self.assertIsNone(result["unit_class"])
        self.assertEqual(result["unit_of_measurement"], "EUR")

    def test_energy_provides_energy_unit_class(self):
        result = load_metadata_function()("demo-contract", "energy", "Test")
        self.assertEqual(result["unit_class"], "energy")
        self.assertEqual(result["unit_of_measurement"], "kWh")


if __name__ == "__main__":
    unittest.main()
