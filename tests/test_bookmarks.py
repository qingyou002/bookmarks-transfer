import copy
import tempfile
import unittest
from pathlib import Path

from browser_transfer.bookmarks import Comparison, StateStore, fingerprint
from browser_transfer.models import MigrationError, Profile


def node(node_id, title, url=None, children=None, **fields):
    value = {"id": str(node_id), "title": title, **fields}
    if url is not None:
        value["url"] = url
    else:
        value["children"] = children or []
    return value


def bar(*children):
    return node("1", "书签栏", children=list(children), folderType="bookmarks-bar")


class BookmarkTests(unittest.TestCase):
    def comparison(self, source, target, state=None):
        return Comparison(source, target, "edge", "chrome", state)

    def test_default_source_layout_and_target_base_noop(self):
        source = bar(node(10, "乙", "https://b.invalid"), node(11, "甲改名", "https://a.invalid"))
        target = bar(node(20, "甲", "https://a.invalid"), node(21, "乙", "https://b.invalid"), node(22, "仅目标", "https://t.invalid"))
        comparison = self.comparison(source, target)
        plan = comparison.resolve({"base": "source"})
        self.assertEqual([n["title"] for n in plan["tree"]], ["乙", "甲改名"])
        self.assertEqual([n["targetId"] for n in plan["tree"]], ["21", "20"])
        self.assertEqual(plan["preview"]["deleted"], 1)
        self.assertTrue(any(d["label"] == "同层顺序变化" for d in comparison.differences))
        no_op = comparison.resolve({"base": "target"})["preview"]
        self.assertEqual((no_op["added"], no_op["updated"], no_op["deleted"]), (0, 0, 0))

    def test_first_run_single_side_does_not_claim_deletion(self):
        comparison = self.comparison(bar(), bar(node(20, "保留", "https://keep.invalid")))
        row = comparison.differences[0]
        self.assertEqual(row["label"], "仅目标存在")
        plan = comparison.resolve({"base": "source", "choices": {row["key"]: "target"}})
        self.assertEqual(plan["preview"]["deleted"], 0)
        self.assertEqual(plan["tree"][0]["targetId"], "20")

    def test_history_identifies_deletion_and_is_symmetric(self):
        source, target = bar(node(10, "A", "https://a.invalid")), bar(node(20, "A", "https://a.invalid"))
        first = self.comparison(source, target)
        key = first.by_id["source"]["10"]
        state = first.state_after(target, {key: "20"})
        deleted = self.comparison(bar(), target, state)
        self.assertEqual(deleted.differences[0]["label"], "来源已删除")
        self.assertEqual(deleted.resolve({"base": "source"})["preview"]["deleted"], 1)
        reverse = Comparison(target, bar(), "chrome", "edge", state)
        self.assertEqual(reverse.differences[0]["label"], "目标已删除")
        # A retained target-only bookmark was never present in the source.
        initial = self.comparison(bar(), target)
        state = initial.state_after(target, {initial.by_id["target"]["20"]: "20"})
        self.assertEqual(self.comparison(bar(), target, state).differences[0]["label"], "仅目标存在")

    def test_url_change_and_folder_rename_use_history(self):
        source = bar(node(10, "文件夹", children=[node(11, "A", "https://old.invalid")]))
        target = bar(node(20, "文件夹", children=[node(21, "A", "https://old.invalid")]))
        original = self.comparison(source, target)
        ids = {k: p["target"] for k, p in original.pairs.items() if k != "root"}
        state = original.state_after(target, ids)
        source["children"][0]["title"] = "改名"
        source["children"][0]["children"][0]["url"] = "https://new.invalid"
        changed = self.comparison(source, target, state)
        plan = changed.resolve({"base": "source"})
        self.assertEqual(plan["tree"][0]["targetId"], "20")
        self.assertEqual(plan["tree"][0]["children"][0]["targetId"], "21")
        self.assertEqual(plan["preview"]["added"], 0)
        self.assertEqual(plan["preview"]["updated"], 2)

    def test_unique_url_move_and_preserved_target_only_append(self):
        source = bar(node(10, "F", children=[node(11, "A", "https://a.invalid")]), node(12, "B", "https://b.invalid"))
        target = bar(node(20, "F"), node(21, "A", "https://a.invalid"), node(22, "T", "https://t.invalid"))
        comparison = self.comparison(source, target)
        self.assertTrue(any("目录移动" in d["label"] for d in comparison.differences))
        key = comparison.by_id["target"]["22"]
        plan = comparison.resolve({"base": "source", "choices": {key: "target"}})
        self.assertEqual([n["title"] for n in plan["tree"]], ["F", "B", "T"])
        self.assertEqual(plan["tree"][0]["children"][0]["targetId"], "21")

    def test_duplicate_urls_and_duplicate_folder_paths_stay_separate(self):
        source = bar(node(10, "A", "https://same.invalid"), node(11, "A", "https://same.invalid"))
        target = bar(node(20, "A", "https://same.invalid"), node(21, "A", "https://same.invalid"))
        plan = self.comparison(source, target).resolve({"base": "source"})
        self.assertEqual((plan["preview"]["added"], plan["preview"]["deleted"]), (2, 2))
        source = bar(node(10, "F", children=[node(11, "A", "https://a.invalid")]), node(12, "F"))
        target = bar(node(20, "F", children=[node(21, "A", "https://a.invalid")]), node(22, "F"))
        plan = self.comparison(source, target).resolve({"base": "source"})
        self.assertTrue(all(n["targetId"] is None for n in plan["tree"]))
        self.assertIsNone(plan["tree"][0]["children"][0]["targetId"])

    def test_folder_exception_keeps_whole_subtree(self):
        source, target = bar(), bar(node(20, "F", children=[node(21, "A", "https://a.invalid")]))
        comparison = self.comparison(source, target)
        folder = comparison.by_id["target"]["20"]
        plan = comparison.resolve({"base": "source", "choices": {folder: "target"}})
        self.assertEqual(plan["preview"]["deleted"], 0)
        self.assertEqual(plan["tree"][0]["children"][0]["title"], "A")
        child = comparison.by_id["target"]["21"]
        self.assertIn(child, comparison.descendant_keys(folder))
        with self.assertRaises(MigrationError):
            comparison.resolve({"base": "source", "choices": {child: "target"}})

    def test_managed_nodes_cannot_be_deleted_or_changed(self):
        target = bar(node(20, "组织", "https://managed.invalid", unmodifiable="managed"))
        comparison = self.comparison(bar(), target)
        with self.assertRaises(MigrationError):
            comparison.resolve({"base": "source"})
        self.assertEqual(comparison.resolve({"base": "target"})["preview"]["updated"], 0)

    def test_empty_and_fingerprint_ignores_dates_but_tracks_order(self):
        self.assertEqual(self.comparison(bar(), bar()).resolve({"base": "source"})["tree"], [])
        original = bar(node(10, "中文🙂", "https://a.invalid"), node(11, "B", "https://b.invalid"))
        changed = copy.deepcopy(original)
        changed["dateAdded"] = 100
        changed["children"][0]["index"] = 8
        self.assertEqual(fingerprint(original), fingerprint(changed))
        changed["children"].reverse()
        self.assertNotEqual(fingerprint(original), fingerprint(changed))

    def test_order_exception_and_cycle_rejected(self):
        source = bar(node(10, "A", "https://a.invalid"), node(11, "B", "https://b.invalid"))
        target = bar(node(20, "B", "https://b.invalid"), node(21, "A", "https://a.invalid"))
        plan = self.comparison(source, target).resolve({"base": "source", "choices": {"order:root": "target"}})
        self.assertEqual(plan["preview"]["updated"], 0)
        source = bar(node(10, "A", children=[node(11, "B")]))
        target = bar(node(21, "B", children=[node(20, "A")]))
        state = {"pairs": [{"edge": "10", "chrome": "20"}, {"edge": "11", "chrome": "21"}]}
        comparison = self.comparison(source, target, state)
        with self.assertRaises(MigrationError):
            comparison.resolve({"base": "source", "choices": {comparison.by_id["source"]["10"]: "target",
                                                                   comparison.by_id["source"]["11"]: "source"}})

    def test_state_keys_reverse_and_restore_all_related_pairs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            edge, chrome = Profile("Edge", root / "edge", "Default"), Profile("Chrome", root / "chrome", "Default")
            store = StateStore(root / "state")
            key = store.key(edge, chrome, "1", "2")
            self.assertEqual(key, store.key(chrome, edge, "2", "1"))
            state = {"version": 1, "pairs": [], "snapshots": {edge.key: bar(), chrome.key: bar()}}
            store.write(key, state)
            before = store.for_profile(chrome.key)
            second_key = store.key(edge, chrome, "3", "4")
            store.write(second_key, state)
            store.restore_profile(chrome.key, before)
            self.assertEqual(store.for_profile(chrome.key), before)
            self.assertFalse(store.path(second_key).exists())
            with self.assertRaises(MigrationError):
                store.path("../other")
