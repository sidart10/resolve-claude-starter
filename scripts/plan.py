#!/usr/bin/env python3
"""Check an edit file and turn it into frame-exact operations. Pure: no Resolve, no media reads.

A bad plan fails here in a second instead of costing you a render. The checker refuses:
- events that name a source the plan doesn't list;
- cuts outside their source (in below 0, or in + dur past the source's duration);
- events that overlap on the timeline (Resolve would silently trim the later clip's head);
- dissolves without room: a centred dissolve needs dur/2 frames of source after the outgoing clip's out point and
  before the incoming clip's in point. Short handles shorten the dissolve (a warning); none turns it into a cut.

Edit file (schema resolve-starter.edit.v1), times in seconds:

    {"schema": "resolve-starter.edit.v1", "fps": 30, "size": [1920, 1080],
     "sources": {"beach": {"path": "media/beach.mov", "duration": 20.0, "audio": true}},
     "events": [{"id": "e1", "src": "beach", "in": 2.0, "rec_in": 0, "dur": 3.0},
                {"id": "e2", "src": "beach", "in": 9.0, "rec_in": 3.0, "dur": 2.5, "zoom": 1.1,
                 "join_in": {"type": "dissolve", "dur": 0.6}}]}

One layout: each event is one clip on V1 (its sound on A1 when the source has audio), centred, with an optional
uniform zoom. Joins are cuts unless join_in says dissolve.

    python3 plan.py edit.json          prints the operations and warnings; exit 1 with every problem listed
    python3 plan.py --self-test        offline checks of the checker
"""

from __future__ import annotations

import json
import os
import sys

SCHEMA = "resolve-starter.edit.v1"


class PlanError(ValueError):
    pass


def frames(sec: float, fps: float) -> int:
    return int(round(float(sec) * fps))


def build(edit: dict, base_dir: str | None = None) -> dict:
    """edit -> {"fps", "size", "clips", "transitions", "warnings", "duration"}. Raises PlanError listing all problems.
    Clip frames: rec (timeline frame from the start), s0..s1 source frames with s1 exclusive (AppendToTimeline's
    endFrame is exclusive)."""
    errs: list[str] = []
    if edit.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}")
    fps = float(edit.get("fps", 30))
    W, H = edit.get("size", [1920, 1080])
    if fps <= 0 or W <= 0 or H <= 0:
        errs.append("fps and size must be positive")
    sources = edit.get("sources") or {}
    srcs = {}
    for src_id, s in sources.items():
        if not s.get("path"):
            errs.append(f"source {src_id}: path is missing")
            continue
        if not isinstance(s.get("duration"), (int, float)) or s["duration"] <= 0:
            errs.append(f"source {src_id}: duration (seconds) is required and must be above 0")
            continue
        path = s["path"] if os.path.isabs(s["path"]) or base_dir is None else os.path.join(base_dir, s["path"])
        srcs[src_id] = {"path": os.path.abspath(path), "n": frames(s["duration"], fps), "audio": s.get("audio", True)}

    clips, transitions, warnings, seen = [], [], [], set()
    for k, ev in enumerate(edit.get("events") or []):
        eid = ev.get("id") or f"#{k + 1}"
        if eid in seen:
            errs.append(f"{eid}: duplicate event id")
        seen.add(eid)
        src_id = ev.get("src")
        if src_id not in sources:
            errs.append(f"{eid}: unknown source {src_id!r} (sources: {sorted(sources)})")
            continue
        if src_id not in srcs:
            continue
        try:
            a, d, r = float(ev["in"]), float(ev["dur"]), float(ev["rec_in"])
        except (KeyError, TypeError, ValueError):
            errs.append(f"{eid}: in, rec_in and dur are required numbers (seconds)")
            continue
        s0, n, rec = frames(a, fps), frames(d, fps), frames(r, fps)
        if n <= 0:
            errs.append(f"{eid}: dur must be at least one frame")
            continue
        if s0 < 0 or s0 + n > srcs[src_id]["n"]:
            errs.append(f"{eid}: {a:.2f}s to {a + d:.2f}s is outside {src_id}'s {sources[src_id]['duration']}s")
        if rec < 0:
            errs.append(f"{eid}: rec_in must be 0 or later")
        zoom = ev.get("zoom", 1.0)
        if not isinstance(zoom, (int, float)) or not 0.1 <= zoom <= 5:
            errs.append(f"{eid}: zoom must be a number from 0.1 to 5 (one value: scale stays uniform)")
            zoom = 1.0
        clips.append({"id": eid, "src": src_id, "rec": rec, "s0": s0, "s1": s0 + n, "zoom": float(zoom),
                      "audio": bool(srcs[src_id]["audio"])})
        j = ev.get("join_in") or {"type": "cut"}
        if j.get("type") not in ("cut", "dissolve"):
            errs.append(f"{eid}: join_in type must be cut or dissolve")
        elif j["type"] == "dissolve":
            transitions.append({"into": eid, "dur": frames(j.get("dur", 1.0), fps)})

    by_rec = sorted(clips, key=lambda c: c["rec"])
    for a, b in zip(by_rec, by_rec[1:]):
        end = a["rec"] + a["s1"] - a["s0"]
        if end > b["rec"]:
            errs.append(f"{a['id']} ends at frame {end}, after {b['id']} starts at {b['rec']} "
                        f"(Resolve would trim {b['id']}'s head)")

    prev = {b["id"]: a for a, b in zip(by_rec, by_rec[1:])}
    cl = {c["id"]: c for c in clips}
    for t in transitions:
        a, b = prev.get(t["into"]), cl.get(t["into"])
        if a is None:
            errs.append(f"{t['into']}: a dissolve needs a clip before it")
            continue
        if a["rec"] + a["s1"] - a["s0"] != b["rec"]:
            warnings.append(f"{t['into']}: dissolve skipped, it does not touch {a['id']} (a gap or an overlap)")
            t["skip"] = True
            continue
        tail, head = srcs[a["src"]]["n"] - a["s1"], b["s0"]
        half = min(t["dur"] // 2, tail, head)
        if half < t["dur"] // 2:
            warnings.append(f"{t['into']}: dissolve shortened from {t['dur']} to {2 * half} frames "
                            f"(source left after {a['id']}: {tail}, before {b['id']}: {head})")
            t["dur"] = 2 * half
        if t["dur"] < 2:
            warnings.append(f"{t['into']}: no handles for a dissolve, it stays a cut")
            t["skip"] = True
        t["outgoing"] = a["id"]

    if not clips and not errs:
        errs.append("events is empty")
    if errs:
        raise PlanError("; ".join(errs))
    return {"fps": fps, "size": [W, H], "sources": srcs, "clips": clips,
            "transitions": [t for t in transitions if not t.get("skip")], "warnings": warnings,
            "duration": max(c["rec"] + c["s1"] - c["s0"] for c in clips)}


def load(path: str) -> dict:
    with open(path) as f:
        edit = json.load(f)
    return build(edit, os.path.dirname(os.path.abspath(path)))


def self_test() -> int:
    good = {"schema": SCHEMA, "fps": 30, "size": [1920, 1080],
            "sources": {"a": {"path": "/x/a.mov", "duration": 10}, "b": {"path": "/x/b.mov", "duration": 4}},
            "events": [{"id": "e1", "src": "a", "in": 1, "rec_in": 0, "dur": 3},
                       {"id": "e2", "src": "b", "in": 1, "rec_in": 3, "dur": 2, "join_in": {"type": "dissolve", "dur": 1}}]}
    ops = build(good)
    assert ops["clips"][1] == {"id": "e2", "src": "b", "rec": 90, "s0": 30, "s1": 90, "zoom": 1.0, "audio": True}
    assert ops["transitions"] == [{"into": "e2", "dur": 30, "outgoing": "e1"}] and not ops["warnings"], ops

    def fails(edit, needle):
        try:
            build(edit)
        except PlanError as e:
            assert needle in str(e), (needle, str(e))
            return
        raise AssertionError(f"expected a PlanError about {needle!r}")

    def variant(**ev2):
        e = json.loads(json.dumps(good))
        e["events"][1].update(ev2)
        return e
    fails(variant(src="nope"), "unknown source")
    fails(variant(**{"in": 3}), "outside b")
    fails(variant(rec_in=2.5), "would trim e2's head")
    fails(variant(zoom=0), "zoom")
    short = build(variant(**{"in": 0.2}))                       # 6 frames of head: the 30-frame dissolve becomes 12
    assert short["transitions"][0]["dur"] == 12 and "shortened" in short["warnings"][0], short
    none = build(variant(**{"in": 0}))
    assert not none["transitions"] and "stays a cut" in none["warnings"][-1], none
    gap = build(variant(rec_in=3.5))
    assert not gap["transitions"] and "does not touch" in gap["warnings"][0], gap
    first = json.loads(json.dumps(good))
    first["events"][0]["join_in"] = {"type": "dissolve"}
    fails(first, "needs a clip before it")
    print("plan self-test: ok (unknown source, out of range, overlap, zoom, short and missing dissolve handles)")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) != 1:
        print(__doc__)
        return 2
    try:
        ops = load(argv[0])
    except PlanError as e:
        print(json.dumps({"ok": False, "errors": str(e).split("; ")}, indent=1))
        return 1
    print(json.dumps({"ok": True, **{k: v for k, v in ops.items() if k != "sources"}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
