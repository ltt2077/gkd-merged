import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.merge import (merge, mapped_key, publish, read_source, validate)


CONFIG = {"id": 20772026, "name": "test", "repository": "owner/repo"}


def group(key=0, rules=None, **kw):
    return {"key": key, "name": "开屏广告-测试", "rules": rules or [{"matches": "[text='跳过']"}], **kw}


def source(namespace, groups, categories=None, global_groups=None):
    return ({"key": f"source{namespace}", "namespace": namespace, "id": namespace},
            {"id": namespace, "version": 1, "name": "test", "categories": categories or [],
             "apps": [{"id": "app.test", "name": "Test", "groups": groups}],
             "globalGroups": global_groups or []})


def total(sub):
    return sum(len(g["rules"]) for a in sub["apps"] for g in a["groups"])


class MergeTests(unittest.TestCase):
    def test_metadata_and_source_ids_do_not_prevent_deduplication(self):
        a = group(rules=[{"key": 8, "matches": "A", "snapshotUrls": "one"}])
        b = group(key=22, rules=[{"key": 90, "matches": ["A"], "snapshotUrls": "two"}], desc="different")
        out, report = merge([source(1, [a]), source(2, [b])], CONFIG)
        self.assertEqual(total(out), 1)
        self.assertEqual(report["removedRules"], 1)

    def test_inherited_properties_and_partial_overlap(self):
        a = group(rules=[{"matches": "A"}], fastQuery=True)
        b = group(rules=[{"matches": "A", "fastQuery": True}, {"matches": "B"}])
        out, report = merge([source(1, [a]), source(2, [b])], CONFIG)
        self.assertEqual(total(out), 2)
        self.assertEqual(report["removedRules"], 1)

    def test_different_actions_limits_and_selector_order_stay_distinct(self):
        variants = [{"action": "back"}, {"activityIds": ["A"]}, {"actionCd": 2000},
                    {"versionCode": ">=12"}, {"position": {"left": "0"}},
                    {"excludeMatches": "B"}, {"matches": ["B", "A"]}, {"matches": ["A", "B"]}]
        base = {"matches": "A"}
        sources = [source(i + 1, [group(rules=[{**base, **v}])]) for i, v in enumerate([{}] + variants)]
        out, _ = merge(sources, CONFIG)
        self.assertEqual(total(out), len(sources))

    def test_string_shorthand(self):
        out, _ = merge([source(1, [group(rules="A")]), source(2, [group(rules=[{"matches": ["A"]}])])], CONFIG)
        self.assertEqual(total(out), 1)

    def test_dependency_chain_rekeys_as_a_unit(self):
        a = group(rules=[{"key": 0, "matches": "A"}, {"key": 1, "preKeys": [0], "matches": "B"}])
        b = group(key=9, rules=[{"key": 44, "matches": "A"}, {"key": 99, "preKeys": 44, "matches": "B"}])
        out, _ = merge([source(1, [a]), source(2, [b])], CONFIG)
        self.assertEqual(total(out), 2)
        self.assertEqual(out["apps"][0]["groups"][0]["rules"][1]["preKeys"], [0])

    def test_chain_not_split_when_only_one_step_overlaps(self):
        a = group(rules=[{"key": 0, "matches": "A"}, {"key": 1, "preKeys": [0], "matches": "B"}])
        b = group(rules=[{"matches": "A"}])
        out, _ = merge([source(1, [a]), source(2, [b])], CONFIG)
        self.assertEqual(total(out), 3)

    def test_scope_keys_and_incoming_references_are_protected(self):
        a = [group(key=5, rules=[{"key": 7, "preKeys": [9], "matches": "A"}], scopeKeys=[8]),
             group(key=8, rules=[{"key": 9, "matches": "B"}])]
        b = [group(key=50, rules=[{"key": 70, "preKeys": [90], "matches": "A"}], scopeKeys=[80]),
             group(key=80, rules=[{"key": 90, "matches": "B"}])]
        out, report = merge([source(1, a), source(2, b), source(3, [group(rules=[{"matches": "B"}])])], CONFIG)
        self.assertEqual(report["removedRules"], 2)
        groups = out["apps"][0]["groups"]
        self.assertEqual(groups[0]["scopeKeys"], [groups[1]["key"]])
        self.assertEqual(total(out), 3)

    def test_category_defaults_do_not_silently_enable_rules(self):
        a = source(1, [group()], [{"key": 0, "name": "开屏广告", "enable": False}])
        b = source(2, [group()], [{"key": 8, "name": "开屏广告", "enable": True}])
        out, _ = merge([a, b], CONFIG)
        self.assertEqual([g["enable"] for g in out["apps"][0]["groups"]], [False, True])
        self.assertNotIn("enable", out["categories"][0])

    def test_global_exclusions_not_lost(self):
        a = group(rules=[{"matches": "A"}], apps=[{"id": "bank.app", "enable": False}], disableIfAppGroupMatch="开屏广告")
        b = group(rules=[{"matches": "A"}], apps=[], disableIfAppGroupMatch="开屏广告")
        out, _ = merge([source(1, [], global_groups=[a]), source(2, [], global_groups=[b])], CONFIG)
        self.assertEqual(len(out["globalGroups"]), 2)
        self.assertEqual(out["globalGroups"][0]["apps"][0]["enable"], False)

    def test_stable_group_keys_and_no_source_mutation(self):
        a = source(1, [group(key=-1)])
        original = copy.deepcopy(a)
        first, _ = merge([a], CONFIG)
        second, _ = merge([source(3, [group(key=99, rules=[{"matches": "other"}])]), a], CONFIG)
        self.assertEqual(a, original)
        key = first["apps"][0]["groups"][0]["key"]
        self.assertIn(key, [g["key"] for g in second["apps"][0]["groups"]])
        self.assertNotEqual(mapped_key(1, -1), mapped_key(1, 0))

    def test_bad_inputs_fail_validation(self):
        for bad in [source(1, [group(scopeKeys=[99])])[1], source(1, [group(), group()])[1]]:
            with self.assertRaises(ValueError):
                validate(bad)

    def test_disabled_informational_placeholders_are_preserved(self):
        out, _ = merge([source(1, [group(rules=[{}], enable=False)])], CONFIG)
        self.assertEqual(out["apps"][0]["groups"][0]["rules"], [{}])
        with self.assertRaises(ValueError):
            merge([source(1, [group(rules=[{}])])], CONFIG)

    def test_empty_groups_can_suppress_global_rules(self):
        placeholder = group()
        placeholder["rules"] = []
        out, _ = merge([source(1, [placeholder, group(key=1, rules=[{"matches": "B"}])])], CONFIG)
        self.assertEqual(out["apps"][0]["groups"][0]["rules"], [])

    def test_version_only_increments_for_content_changes(self):
        out, report = merge([source(1, [group()])], CONFIG)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertTrue(publish(out, report, root, "day1"))
            self.assertFalse(publish(out, report, root, "day2"))
            self.assertEqual(out["version"], 1)
            out["apps"][0]["groups"][0]["rules"][0]["matches"] = "new"
            self.assertTrue(publish(out, report, root, "day3"))
            self.assertEqual(out["version"], 2)

    def test_network_failure_retains_last_good_source(self):
        conf, data = source(1, [group()])
        conf["url"] = "https://example.invalid/source"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / "sources/cache/source1.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps(data))
            with patch("scripts.merge.urllib.request.urlopen", side_effect=OSError("offline")), patch("scripts.merge.time.sleep"):
                got, status, replacement = read_source(conf, root)
                self.assertEqual(got, data)
                self.assertEqual(status["status"], "stale-cache")
                self.assertIsNone(replacement)
                cache.unlink()
                with self.assertRaises(RuntimeError):
                    read_source(conf, root)


if __name__ == "__main__":
    unittest.main()
