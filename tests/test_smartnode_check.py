#!/usr/bin/env python3

"""Small standard-library test suite for the Yerbas smartnode checker."""

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_NAME = "yerbas_smartnode_check"
MODULE_PATH = Path(__file__).resolve().parents[1] / "yerbas-smartnode-check.py"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
# Python 3.14 dataclasses resolves annotations through sys.modules while the
# class decorator runs, so register dynamically loaded modules before exec.
sys.modules[MODULE_NAME] = MODULE
SPEC.loader.exec_module(MODULE)


class AddressParsingTests(unittest.TestCase):
    def test_ipv4_with_port(self):
        self.assertEqual(MODULE.parse_address("203.0.113.10:15420", 15420), ("203.0.113.10", 15420))

    def test_ipv4_default_port(self):
        self.assertEqual(MODULE.parse_address("203.0.113.10", 15420), ("203.0.113.10", 15420))

    def test_ipv6_with_port(self):
        self.assertEqual(MODULE.parse_address("[2001:db8::1]:15420", 15420), ("2001:db8::1", 15420))

    def test_invalid_port(self):
        with self.assertRaises(ValueError):
            MODULE.parse_address("203.0.113.10:70000", 15420)


class ProtocolTests(unittest.TestCase):
    def test_protocol_number(self):
        self.assertEqual(MODULE.parse_protocol("70223"), 70223)

    def test_invalid_protocol(self):
        self.assertIsNone(MODULE.parse_protocol("UNKNOWN"))


class AlertTests(unittest.TestCase):
    def test_reachability_alert(self):
        report = {"summary": {"reachability_percent": 80, "outdated_protocol": 0, "wrong_port": 0}, "smartnodes": []}
        lines = MODULE.alert_lines(report, None, 95)
        self.assertTrue(any("below" in line for line in lines))

    def test_detects_recovery(self):
        previous = {"smartnodes": [{"outpoint": "a", "port_open": False}]}
        report = {
            "summary": {"reachability_percent": 100, "outdated_protocol": 0, "wrong_port": 0},
            "smartnodes": [{"outpoint": "a", "port_open": True}],
        }
        lines = MODULE.alert_lines(report, previous, 95)
        self.assertTrue(any("recovered" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
