"""Offline purge-order regression tests; no credentials or API calls are used."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import purge_all
from purge.engine import Report, purge_policy_family
from purge.families import ALL_KEYS, NON_POLICY_TYPES, POLICY_FAMILIES, lookup


class PurgeOrderTests(unittest.TestCase):
    def test_all_policies_precede_dependencies(self):
        self.assertEqual(len(POLICY_FAMILIES), 18)
        self.assertEqual(len(ALL_KEYS), len(set(ALL_KEYS)))
        for family in POLICY_FAMILIES:
            for dep in NON_POLICY_TYPES:
                with self.subTest(policy=family.key, dependency=dep.key):
                    self.assertLess(ALL_KEYS.index(family.key), ALL_KEYS.index(dep.key))

    def test_dependency_edges_and_ranges_last(self):
        for referrer, dependency in [
            ("dynamic_ip_allocation", "global_ip_ranges"),
            ("groups", "sites"),
            ("groups", "app_connectors"),
            ("private_apps", "app_connectors"),
            ("app_connectors", "sites"),
            ("sites", "dhcp_relay_groups"),
            ("subscription_groups", "mailing_lists"),
            ("subscription_groups", "webhooks"),
        ]:
            with self.subTest(referrer=referrer, dependency=dependency):
                self.assertLess(ALL_KEYS.index(referrer), ALL_KEYS.index(dependency))
        self.assertEqual(ALL_KEYS[-1], "global_ip_ranges")

    def run_orchestrator(self, *args, failing_key=None):
        visited = []

        def run_one(profile, key, execute):
            visited.append(key)
            report = Report(key)
            if key == failing_key:
                report.record("publish", key, False, "revision failed")
            return report

        with patch.object(sys, "argv", ["purge_all.py", "--profile", "test", *args]), \
                patch.object(purge_all, "guard_profile", return_value={"account_id": "123"}), \
                patch.object(purge_all, "set_config_dir"), \
                patch.object(purge_all, "log"), \
                patch.object(purge_all, "run_one", side_effect=run_one):
            result = purge_all.main()
        return result, visited

    def test_default_run_includes_dynamic_ip_allocation_before_ranges(self):
        result, visited = self.run_orchestrator()
        self.assertEqual(result, 0)
        self.assertEqual(visited, ALL_KEYS)
        self.assertIn("dynamic_ip_allocation", visited)
        self.assertEqual(visited[-1], "global_ip_ranges")

    def test_only_preserves_safe_order_not_argument_order(self):
        result, visited = self.run_orchestrator(
            "--only", "global_ip_ranges,dhcp_relay_groups,dynamic_ip_allocation")
        self.assertEqual(result, 0)
        self.assertEqual(visited, [
            "dynamic_ip_allocation", "dhcp_relay_groups", "global_ip_ranges"])

    def test_policy_failure_stops_before_ranges(self):
        result, visited = self.run_orchestrator(failing_key="dynamic_ip_allocation")
        self.assertEqual(result, 1)
        self.assertNotIn("global_ip_ranges", visited)

    def test_dynamic_ip_rules_removed_and_published(self):
        family = lookup("dynamic_ip_allocation")
        data = {"data": {"policy": {"dynamicIpAllocation": {"policy": {
            "rules": [{"rule": {"id": "rule-1", "name": "EMEA-DYNAMIC"}}],
            "sections": [{"section_id": "section-1", "section_name": "Regional"}],
        }}}}}
        report = Report(family.key)
        with patch("purge.engine.export_json", return_value=data), \
                patch("purge.engine.run_catocli", return_value=(True, "")) as cli:
            purge_policy_family("test", family, report)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual([call.args[4] for call in cli.call_args_list], [
            "removeRule", "removeSection", "publishPolicyRevision"])
        rule_payload = json.loads(cli.call_args_list[0].args[-1])
        self.assertEqual(rule_payload["dynamicIpAllocationRemoveRuleInput"],
                         {"id": "rule-1"})


if __name__ == "__main__":
    unittest.main()
