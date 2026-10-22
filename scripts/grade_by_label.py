#!/usr/bin/env python3
"""Colour by label, never by node number: apply a saved node tree (.drx) to clips, read the labels back, and write
CDL, LUT and node on/off only to labelled nodes.

You build the tree once by hand in the Color page, label every node (right-click a node > Node Label), grab a still
and export it from the Gallery as a .drx. Next to it goes a small tree file that says which labels to expect and
what a script may write to each:

    {"drx": "my-tree-v01.drx",
     "nodes": [{"label": "idt",  "scriptable": []},
               {"label": "bal",  "scriptable": ["cdl", "enabled"]},
               {"label": "look", "scriptable": ["lut", "enabled"]},
               {"label": "odt",  "scriptable": []}]}

A writes file says what to set, for every clip ("*") or by clip name:

    {"*":        {"tex": {"enabled": false}},
     "red.mov":  {"bal": {"cdl": {"slope": [1.05, 1, 0.95], "offset": [0, 0, 0], "power": [1, 1, 1],
                                  "saturation": 1.0}}}}

    python3 grade_by_label.py labels --project ZZ-x --timeline "EDIT v01" [--clip NAME]
    python3 grade_by_label.py apply  --project ZZ-x --timeline "EDIT v01" --tree my-tree-v01.json \\
                                     [--writes writes.json] [--track 1] [--clip NAME ...]
    python3 grade_by_label.py --self-test

Rules it enforces: it never creates, deletes or relabels nodes (the API can't add nodes, and a script shouldn't);
every node index comes from GetNodeLabel after the DRX is applied, never an assumed "node 1"; the graph is fetched
again after ApplyGradeFromDRX because the handle that applied it can read 0 nodes (Resolve 21.1.1); a label
mismatch, a write to a label the tree doesn't mark scriptable, or a node count that changes fails the job.
LUT paths are relative to Resolve's LUT folder (SetLUT refuses anything else). ZZ- projects only unless
--allow-real-project. Runs in a child process under a watchdog, inside resolve_turn.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session import ResolveError, open_project, require_zz, resolve, resolve_turn, run_with_watchdog  # noqa: E402

WRITES = ("cdl", "lut", "enabled")


# ------------------------------------------------------------------------------------------------ pure checks
def load_tree(path: str) -> dict:
    with open(path) as f:
        tree = json.load(f)
    errs = []
    labels = [n.get("label", "") for n in tree.get("nodes") or []]
    if not labels:
        errs.append("nodes is empty")
    for n in tree.get("nodes") or []:
        lab = n.get("label", "")
        if not lab or lab != lab.strip():
            errs.append(f"label {lab!r} must be set, without spaces around it")
        bad = [w for w in n.get("scriptable", []) if w not in WRITES]
        if bad:
            errs.append(f"{lab}: scriptable {bad} is not allowed (only {list(WRITES)}; curves, windows and "
                        "qualifiers are hand work)")
    dup = sorted({x for x in labels if labels.count(x) > 1})
    if dup:
        errs.append(f"duplicate labels {dup}")
    drx = os.path.join(os.path.dirname(os.path.abspath(path)), tree.get("drx", ""))
    if not drx.endswith(".drx") or not os.path.isfile(drx):
        errs.append(f"drx {tree.get('drx')!r} is not a .drx file next to the tree file")
    if errs:
        raise ResolveError(f"tree {os.path.basename(path)}: " + "; ".join(errs))
    tree["drx_path"] = drx
    return tree


def label_mismatch(tree: dict, labels: list[str]) -> list[str]:
    want = [n["label"] for n in tree["nodes"]]
    if labels == want:
        return []
    out = [f"graph has {len(labels)} nodes, the tree has {len(want)}"] if len(labels) != len(want) else []
    out += [f"node {i + 1}: graph {g!r}, tree {w!r}" for i, (g, w) in enumerate(zip(labels, want)) if g != w]
    return out or [f"labels {labels} != {want}"]


def writes_for(writes: dict, clip: str) -> dict:
    """The "*" writes merged with this clip's own (the clip's win, per label and per setting)."""
    out = {lab: dict(spec) for lab, spec in (writes.get("*") or {}).items()}
    for lab, spec in (writes.get(clip) or {}).items():
        out.setdefault(lab, {}).update(spec)
    return out


def write_errors(tree: dict, writes: dict) -> list[str]:
    allow = {n["label"]: set(n.get("scriptable", [])) for n in tree["nodes"]}
    errs = []
    for clip, labels in writes.items():
        for lab, spec in labels.items():
            if lab not in allow:
                errs.append(f"{clip}: the tree has no node labelled {lab!r} (has {sorted(allow)})")
                continue
            for w in spec:
                if w not in WRITES:
                    errs.append(f"{clip} {lab}: {w!r} cannot be scripted (only {list(WRITES)})")
                elif w not in allow[lab]:
                    errs.append(f"{clip} {lab}: the tree does not allow scripted {w}")
            c = spec.get("cdl")
            if c is not None:
                for k in ("slope", "offset", "power"):
                    v = c.get(k, [0, 0, 0] if k == "offset" else [1, 1, 1])
                    if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)):
                        errs.append(f"{clip} {lab}: cdl {k} must be three numbers")
            if "enabled" in spec and not isinstance(spec["enabled"], bool):
                errs.append(f"{clip} {lab}: enabled must be true or false")
    return errs


def node_labels(graph) -> list[str]:
    return [graph.GetNodeLabel(i) or "" for i in range(1, (graph.GetNumNodes() or 0) + 1)]


def grade_item(item, tree: dict, writes: dict) -> dict:
    """Apply the tree to one timeline item, check its labels, write by label. Returns what was done."""
    name = item.GetName()
    g = item.GetNodeGraph()
    if g is None:
        raise ResolveError(f"{name}: no node graph")
    if not g.ApplyGradeFromDRX(tree["drx_path"], 0):
        raise ResolveError(f"{name}: ApplyGradeFromDRX refused")
    g = item.GetNodeGraph()          # the applying handle can read 0 nodes afterwards (21.1.1): fetch it again
    labels = node_labels(g)
    bad = label_mismatch(tree, labels)
    if bad:
        raise ResolveError(f"{name}: after applying the tree the labels do not match: " + "; ".join(bad))
    idx = {lab: i + 1 for i, lab in enumerate(labels)}
    done = []
    for lab, spec in writes_for(writes, name).items():
        i = idx[lab]
        if "cdl" in spec:
            c = spec["cdl"]
            ok = item.SetCDL({"NodeIndex": str(i), "Slope": " ".join(map(str, c.get("slope", [1, 1, 1]))),
                              "Offset": " ".join(map(str, c.get("offset", [0, 0, 0]))),
                              "Power": " ".join(map(str, c.get("power", [1, 1, 1]))),
                              "Saturation": str(c.get("saturation", 1.0))})
            if not ok:
                raise ResolveError(f"{name}: SetCDL on {lab} (node {i}) refused")
        if spec.get("lut") and not g.SetLUT(i, spec["lut"]):
            raise ResolveError(f"{name}: SetLUT({spec['lut']!r}) on {lab} (node {i}) refused; the path must be "
                               "relative to Resolve's LUT folder")
        if "enabled" in spec and not g.SetNodeEnabled(i, bool(spec["enabled"])):
            raise ResolveError(f"{name}: SetNodeEnabled on {lab} (node {i}) refused")
        if (g.GetNumNodes() or 0) != len(idx):
            raise ResolveError(f"{name}: the node count changed after writing {lab}; scripts never add nodes")
        done.append({"node": lab, "index": i, "set": sorted(spec)})
    after = node_labels(item.GetNodeGraph())
    if label_mismatch(tree, after):
        raise ResolveError(f"{name}: labels changed during the writes: {after}")
    return {"clip": name, "verified": True, "labels": after, "writes": done}


# ------------------------------------------------------------------------------------------------ Resolve side
def _timeline(project, name: str):
    for i in range(1, project.GetTimelineCount() + 1):
        tl = project.GetTimelineByIndex(i)
        if tl.GetName() == name:
            project.SetCurrentTimeline(tl)
            return tl
    raise ResolveError(f"no timeline {name!r} in {project.GetName()}")


def _items(tl, track: int, clips: list[str] | None):
    items = [it for it in tl.GetItemListInTrack("video", track) or [] if it.GetNodeGraph() is not None]
    if clips:
        items = [it for it in items if it.GetName() in clips]
        missing = set(clips) - {it.GetName() for it in items}
        if missing:
            raise ResolveError(f"clips not on video track {track}: {sorted(missing)}")
    if not items:
        raise ResolveError(f"no gradable clips on video track {track}")
    return items


def child(a) -> dict:
    with resolve_turn(f"grade {a.cmd} {a.timeline}"):
        p = open_project(a.project)
        tl = _timeline(p, a.timeline)
        items = _items(tl, a.track, a.clip)
        if a.cmd == "labels":
            return {"ok": True, "clips": [{"clip": it.GetName(), "labels": node_labels(it.GetNodeGraph())}
                                          for it in items]}
        tree = load_tree(a.tree)
        writes = {}
        if a.writes:
            with open(a.writes) as f:
                writes = json.load(f)
        errs = write_errors(tree, writes)
        if errs:
            raise ResolveError("; ".join(errs))
        out = [grade_item(it, tree, writes) for it in items]
        resolve().GetProjectManager().SaveProject()
        return {"ok": all(r["verified"] for r in out), "tree": os.path.basename(a.tree), "clips": out}


# ------------------------------------------------------------------------------------------------ self-test
class _Graph:
    def __init__(self, item, stale=False):
        self.item, self.stale = item, stale

    def GetNumNodes(self):
        return 0 if self.stale else len(self.item.labels)

    def GetNodeLabel(self, i):
        return self.item.labels[i - 1]

    def ApplyGradeFromDRX(self, path, mode):
        self.item.labels = list(self.item.drx_labels)
        self.stale = True                       # like 21.1.1: this handle now reads 0 nodes
        return True

    def SetLUT(self, i, path):
        self.item.calls.append(("lut", i, path))
        return True

    def SetNodeEnabled(self, i, on):
        self.item.calls.append(("enabled", i, on))
        if self.item.grow:
            self.item.labels.append("extra")
        return True


class _Item:
    def __init__(self, drx_labels, grow=False):
        self.labels, self.drx_labels, self.calls, self.grow = ["corrector 1"], drx_labels, [], grow

    def GetName(self):
        return "red.mov"

    def GetNodeGraph(self):
        return _Graph(self)

    def SetCDL(self, cdl):
        self.calls.append(("cdl", int(cdl["NodeIndex"]), cdl["Slope"]))
        return True


def self_test() -> int:
    import tempfile
    labels = ["idt", "bal", "adj", "look", "tex", "odt"]
    allow = {"bal": ["cdl", "enabled"], "adj": ["cdl", "enabled"], "look": ["lut", "enabled"], "tex": ["enabled"]}
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "t.drx"), "w").write("<fake/>")
        tp = os.path.join(d, "t.json")
        json.dump({"drx": "t.drx", "nodes": [{"label": x, "scriptable": allow.get(x, [])} for x in labels]},
                  open(tp, "w"))
        tree = load_tree(tp)
        writes = {"*": {"tex": {"enabled": False}}, "red.mov": {"bal": {"cdl": {"slope": [1.1, 1, 0.9]}}}}
        assert not write_errors(tree, writes)
        it = _Item(labels)
        r = grade_item(it, tree, writes)
        assert r["verified"] and ("enabled", 5, False) in it.calls and ("cdl", 2, "1.1 1 0.9") in it.calls, it.calls
        assert any("does not allow" in e for e in write_errors(tree, {"*": {"idt": {"lut": "x.cube"}}}))
        assert any("no node labelled" in e for e in write_errors(tree, {"*": {"grain": {"enabled": True}}}))
        assert any("cannot be scripted" in e for e in write_errors(tree, {"*": {"adj": {"curves": 1}}}))
        for drx_labels, needle in ((["idt", "adj", "bal", "look", "tex", "odt"], "node 2"),
                                   (labels[:5], "graph has 5")):
            try:
                grade_item(_Item(drx_labels), tree, {})
                raise AssertionError("a label mismatch must fail")
            except ResolveError as e:
                assert needle in str(e), e
        try:
            grade_item(_Item(labels, grow=True), tree, writes)
            raise AssertionError("a node count change must fail")
        except ResolveError as e:
            assert "node count changed" in str(e), e
        json.dump({"drx": "t.drx", "nodes": [{"label": "a"}, {"label": "a"}]}, open(tp, "w"))
        try:
            load_tree(tp)
            raise AssertionError("duplicate labels must fail")
        except ResolveError as e:
            assert "duplicate" in str(e)
    print("grade self-test: ok (re-fetch after apply, write by label, refusals, mismatch, node-count guard)")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("labels", "apply"))
    ap.add_argument("--project", required=True)
    ap.add_argument("--timeline", required=True)
    ap.add_argument("--tree", help="tree file (apply)")
    ap.add_argument("--writes", help="writes file (apply)")
    ap.add_argument("--track", type=int, default=1)
    ap.add_argument("--clip", nargs="*", help="only these clip names")
    ap.add_argument("--allow-real-project", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--result", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.child:
        try:
            res = child(a)
        except ResolveError as e:
            res = {"ok": False, "error": str(e)}
        except Exception as e:                       # an API surprise: report it instead of dying silently
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with open(a.result, "w") as f:
            json.dump(res, f, indent=1)
        return 0 if res.get("ok") else 1
    try:
        if not a.allow_real_project:
            require_zz(a.project)
        if a.cmd == "apply":
            if not a.tree:
                raise ResolveError("apply needs --tree")
            a.tree = os.path.abspath(a.tree)
            tree = load_tree(a.tree)
            if a.writes:
                a.writes = os.path.abspath(a.writes)
                with open(a.writes) as f:
                    errs = write_errors(tree, json.load(f))
                if errs:
                    raise ResolveError("; ".join(errs))
    except (ResolveError, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=1))
        return 1
    import tempfile
    result = os.path.join(tempfile.gettempdir(), f"grade-result-{os.getpid()}.json")
    fwd = [x for x in argv]
    for flag, val in (("--tree", a.tree), ("--writes", a.writes)):
        if val and flag in fwd:
            fwd[fwd.index(flag) + 1] = val
    try:
        code = run_with_watchdog([sys.executable, os.path.abspath(__file__), *fwd, "--child", "--result", result],
                                 a.timeout)
        with open(result) as f:
            res = json.load(f)
        os.remove(result)
    except ResolveError as e:
        res = {"ok": False, "error": str(e)}
    except (OSError, ValueError):
        res = {"ok": False, "error": f"the Resolve job exited {code} without a result"}
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
