"""Bookmark bar comparison shared by the desktop and command-line interfaces.

Browser IDs are profile-local. Pair records are symmetric so reversing direction
uses the same identities, without treating ordinary bar nodes as archive copies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from .backup import write_json
from .models import MigrationError, Profile
from .windows import private_dir


def fingerprint(node: dict) -> str:
    def shape(n):
        return [str(n["id"]), n.get("title", ""), n.get("url"), n.get("unmodifiable"),
                [shape(c) for c in n.get("children", [])]]
    raw = json.dumps(shape(node), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def select_bar(snapshot: dict, bar_id: str) -> dict:
    bar = next((b for b in snapshot.get("bars", []) if b["id"] == bar_id), None)
    if bar is None:
        raise MigrationError("所选书签栏已不存在，请重新比较。")
    if bar.get("unmodifiable"):
        raise MigrationError("所选书签栏受管理，无法迁移，请选择可编辑的书签栏。")
    return bar


class StateStore:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def key(source: Profile, target: Profile, source_bar: str, target_bar: str) -> str:
        identity = sorted([(source.key, source_bar), (target.key, target_bar)])
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def path(self, key: str) -> Path:
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise MigrationError("书签比较状态标识无效。")
        return self.root / (key + ".json")

    def read(self, key: str) -> dict | None:
        path = self.path(key)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value is None:
                raise MigrationError("书签比较状态文件为空，无法读取。")
            self.validate(value)
            return value
        except (OSError, ValueError, TypeError, KeyError):
            raise MigrationError("书签比较状态无法读取，请恢复备份后重试。") from None

    @staticmethod
    def validate(value):
        if value is not None and (not isinstance(value, dict) or value.get("version") != 1 or
                                   not isinstance(value.get("pairs"), list) or
                                   not isinstance(value.get("snapshots"), dict)):
            raise MigrationError("书签比较状态格式不受支持。")
        if value is not None and any(not isinstance(pair, dict) or any(
                not isinstance(key, str) or (node_id is not None and not isinstance(node_id, str))
                for key, node_id in pair.items()) for pair in value["pairs"]):
            raise MigrationError("书签比较映射格式不受支持。")

    def write(self, key: str, value: dict | None):
        path = self.path(key)
        self.validate(value)
        if value is None:
            path.unlink(missing_ok=True)
        else:
            private_dir(self.root)
            write_json(path, value)

    def for_profile(self, profile_key: str) -> dict:
        result = {}
        if self.root.exists():
            for path in self.root.glob("*.json"):
                state = self.read(path.stem)
                if profile_key in state["snapshots"]:
                    result[path.stem] = state
        return result

    def validate_profile_states(self, profile_key: str, states: dict):
        if not isinstance(states, dict):
            raise MigrationError("备份中的书签比较状态格式无效。")
        for key, state in states.items():
            self.path(key)
            self.validate(state)
            if state is None or profile_key not in state["snapshots"]:
                raise MigrationError("备份中的书签比较状态与目标配置不符。")

    def restore_profile(self, profile_key: str, states: dict):
        self.validate_profile_states(profile_key, states)
        current = self.for_profile(profile_key)
        try:
            for key in set(current) | set(states):
                self.write(key, states.get(key))
        except Exception:
            for key in set(current) | set(states):
                self.write(key, current.get(key))
            raise


class Comparison:
    def __init__(self, source: dict, target: dict, source_key: str, target_key: str,
                 state: dict | None = None):
        self.source, self.target = copy.deepcopy(source), copy.deepcopy(target)
        self.profile_keys = {"source": source_key, "target": target_key}
        self.nodes, self.parents, self.paths = {}, {}, {}
        for side, root in (("source", source), ("target", target)):
            nodes, parents, paths = {}, {}, {}
            def walk(node, parent=None, path=()):
                node_id = str(node["id"])
                if node_id in nodes:
                    raise MigrationError("书签树包含重复标识，无法比较。")
                nodes[node_id], parents[node_id], paths[node_id] = node, parent, path
                for child in node.get("children", []):
                    walk(child, node_id, path + (child.get("title", ""),))
            walk(root)
            self.nodes[side], self.parents[side], self.paths[side] = nodes, parents, paths
        self.pairs = {"root": {"source": source["id"], "target": target["id"]}}
        self.by_id = {"source": {source["id"]: "root"}, "target": {target["id"]: "root"}}
        self.historical = set()

        def pair(left, right, historical=False):
            key = f"node-{len(self.pairs)}"
            self.pairs[key] = {"source": left, "target": right}
            for side, node_id in (("source", left), ("target", right)):
                if node_id is not None:
                    self.by_id[side][node_id] = key
            if historical:
                self.historical.add(key)

        # Reserve surviving halves of old pairs: a deleted item must not be
        # silently rebound to a new, unrelated bookmark with the same URL.
        for old in (state or {}).get("pairs", []):
            left, right = old.get(source_key), old.get(target_key)
            if left is None or right is None:
                continue
            left = left if left in self.nodes["source"] else None
            right = right if right in self.nodes["target"] else None
            if left in self.by_id["source"] or right in self.by_id["target"]:
                continue
            if left and right and bool(self.nodes["source"][left].get("url")) != bool(self.nodes["target"][right].get("url")):
                continue
            if left or right:
                pair(left, right, True)

        def match(signature, predicate):
            indexes = {}
            for side in ("source", "target"):
                index = defaultdict(list)
                for node_id, node in self.nodes[side].items():
                    if node_id not in self.by_id[side] and predicate(node):
                        index[signature(side, node_id, node)].append(node_id)
                indexes[side] = index
            for identity, left in indexes["source"].items():
                right = indexes["target"].get(identity, [])
                if len(left) == len(right) == 1:
                    pair(left[0], right[0])

        # Duplicate folder paths remain independent, including their children.
        folder_paths = {}
        for side in ("source", "target"):
            counts = defaultdict(int)
            for node_id, node in self.nodes[side].items():
                if "url" not in node:
                    counts[self.paths[side][node_id]] += 1
            folder_paths[side] = counts
        def unambiguous_path(side, node_id):
            path = self.paths[side][node_id]
            return all(folder_paths[side][path[:i]] <= 1 for i in range(1, len(path) + 1))
        match(lambda s, i, n: self.paths[s][i] if unambiguous_path(s, i) else (s, i),
              lambda n: "url" not in n)
        match(lambda s, i, n: (self.paths[s][i][:-1], n.get("url")) if
              unambiguous_path(s, i) else (s, i), lambda n: "url" in n)
        # Unique URLs can identify a first-run move/rename; duplicates cannot.
        totals = {s: defaultdict(int) for s in ("source", "target")}
        for side in totals:
            for node in self.nodes[side].values():
                if "url" in node:
                    totals[side][node["url"]] += 1
        match(lambda s, i, n: n["url"] if all(t[n["url"]] == 1 for t in totals.values())
              and unambiguous_path(s, i) else (s, i), lambda n: "url" in n)
        for side in ("source", "target"):
            for node_id in self.nodes[side]:
                if node_id not in self.by_id[side]:
                    pair(node_id if side == "source" else None, node_id if side == "target" else None)
        self.differences = self._differences()

    def node(self, key, side):
        return self.nodes[side].get(self.pairs[key][side])

    def parent(self, key, side):
        node_id = self.pairs[key][side]
        return self.by_id[side].get(self.parents[side].get(node_id))

    def children(self, key, side):
        node = self.node(key, side) or {}
        return [self.by_id[side][c["id"]] for c in node.get("children", [])]

    def describe(self, key, side):
        node = self.node(key, side)
        if node is None:
            return "（不存在）"
        path = " / ".join(self.paths[side][node["id"]]) or node.get("title", "书签栏")
        return path + ("  ·  " + node["url"] if "url" in node else "")

    def _differences(self):
        rows = []
        for key, pair in self.pairs.items():
            left, right = self.node(key, "source"), self.node(key, "target")
            reasons = []
            if key != "root":
                if left is None:
                    reasons.append("来源已删除" if key in self.historical else "仅目标存在")
                elif right is None:
                    reasons.append("目标已删除" if key in self.historical else "仅来源存在")
                else:
                    if left.get("title", "") != right.get("title", ""):
                        reasons.append("名称变化")
                    if left.get("url") != right.get("url"):
                        reasons.append("网址变化")
                    if self.parent(key, "source") != self.parent(key, "target"):
                        reasons.append("目录移动")
            if reasons:
                rows.append({"key": key, "label": "、".join(reasons),
                             "source": self.describe(key, "source"), "target": self.describe(key, "target")})
            if left is not None and right is not None and "url" not in left:
                l, r = self.children(key, "source"), self.children(key, "target")
                common = set(l) & set(r)
                if [i for i in l if i in common] != [i for i in r if i in common]:
                    rows.append({"key": "order:" + key, "label": "同层顺序变化",
                                 "source": self.describe(key, "source"), "target": self.describe(key, "target")})
        return rows

    def descendant_keys(self, key):
        result = set()
        def visit(parent):
            for side in ("source", "target"):
                for child in self.children(parent, side):
                    if child != key and child not in result:
                        result.add(child)
                        visit(child)
        visit(key)
        return result

    def resolve(self, decision: dict) -> dict:
        base = decision.get("base", "source")
        choices = decision.get("choices", {})
        valid = set(self.pairs) | {"order:" + k for k in self.pairs}
        if base not in {"source", "target"} or not isinstance(choices, dict) or any(
                k not in valid or v not in {"source", "target"} for k, v in choices.items()):
            raise MigrationError("书签差异选择无效。")

        def side_for(key):
            if key in choices:
                return choices[key]
            # A folder exception switches its subtree's default. Explicit child
            # choices can then be made again by the user.
            side = base if self.node(key, base) is not None else ("target" if base == "source" else "source")
            parent, visited = self.parent(key, side), set()
            while parent and parent not in visited:
                visited.add(parent)
                if parent in choices:
                    return choices[parent]
                parent = self.parent(parent, side)
            return base

        selected = {}
        for key in self.pairs:
            if key == "root":
                continue
            side = side_for(key)
            node = self.node(key, side)
            if node is not None:
                selected[key] = {"key": key, "title": node.get("title", ""),
                                 "parent": self.parent(key, side), "side": side,
                                 "targetId": self.pairs[key]["target"]}
                if "url" in node:
                    selected[key]["url"] = node["url"]
        for key, node in selected.items():
            parent = node["parent"]
            if parent != "root" and (parent not in selected or "url" in selected[parent]):
                raise MigrationError("保留的条目缺少文件夹；请先保留其父文件夹或取消该条目例外。")
            visited = {key}
            while parent != "root":
                if parent in visited:
                    raise MigrationError("所选目录移动形成循环，请调整目录选择。")
                visited.add(parent)
                parent = selected[parent]["parent"]

        def build(parent):
            side = choices.get("order:" + parent, base if parent == "root" else side_for(parent))
            other = "target" if side == "source" else "source"
            order = []
            for child in self.children(parent, side) + self.children(parent, other) + list(selected):
                if child in selected and selected[child]["parent"] == parent and child not in order:
                    order.append(child)
            result = []
            for child in order:
                node = selected[child]
                clean = {k: v for k, v in node.items() if k not in {"parent", "side"}}
                if "url" not in clean:
                    clean["children"] = build(child)
                result.append(clean)
            return result

        tree = build("root")
        desired_positions = {}
        def positions(nodes, parent):
            for index, node in enumerate(nodes):
                desired_positions[node["key"]] = (parent, index)
                positions(node.get("children", []), node["key"])
        positions(tree, "root")
        counts = {"added": 0, "updated": 0, "moved": 0, "deleted": 0, "skipped": 0}
        for key in self.pairs:
            if key == "root":
                continue
            old, desired = self.node(key, "target"), selected.get(key)
            if old is not None and desired is None:
                if old.get("unmodifiable"):
                    raise MigrationError("受管理的目标条目不能删除，请选择保留目标版本。")
                counts["deleted"] += 1
            elif desired is not None and old is None:
                counts["added"] += 1
            elif desired is not None:
                parent = self.parent(key, "target")
                old_position = (parent, self.children(parent, "target").index(key))
                moved = old_position != desired_positions[key]
                changed = old.get("title", "") != desired["title"] or old.get("url") != desired.get("url")
                if old.get("unmodifiable") and (moved or changed):
                    raise MigrationError("受管理的目标条目不能修改，请保留目标内容和顺序。")
                counts["moved"] += int(moved)
                counts["updated" if moved or changed else "skipped"] += 1
        return {"barId": self.target["id"], "expected": fingerprint(self.target),
                "tree": tree, "preview": counts}

    def state_after(self, target_bar: dict, node_ids: dict) -> dict:
        source_key, target_key = self.profile_keys["source"], self.profile_keys["target"]
        pairs = []
        for key, pair in self.pairs.items():
            if key != "root" and (pair["source"] is not None or key in node_ids):
                pairs.append({source_key: pair["source"], target_key: node_ids.get(key)})
        return {"version": 1, "pairs": pairs,
                "snapshots": {source_key: self.source, target_key: target_bar}}
