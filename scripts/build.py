#!/usr/bin/env python3
"""Build a NEW Resolve timeline from an edit file, read every clip back, render it and check the file's colour tags.

    python3 build.py edit.json --project ZZ-my-test --timeline "EDIT v01" --out /absolute/render/dir

What it does, in order:
1. Checks the plan (plan.py). A bad plan stops here, before Resolve is touched.
2. In a child process under a watchdog, inside resolve_turn (lock; your open project is saved and reopened after):
   opens the project (a missing ZZ- project is created with plain DaVinci YRGB colour science, size and fps set on
   the PROJECT before any timeline exists), refuses a timeline name that already exists, imports the sources,
   creates the timeline and appends every clip with AppendToTimeline (never the Insert*IntoTimeline calls).
3. Reads every placed clip back (record frames, source in) and fails if any differs from the plan.
4. Renders mp4/H.264 to an absolute path with Color Space Tag Rec.709 and Gamma Tag Rec.709-A, then checks with
   ffprobe that the file reads bt709,bt709,bt709 (a Gamma 2.4 tag plays washed out on Macs and phones) and has the
   planned number of frames.

Prints one JSON result. Exit 0 only when the build, the read-back and the render checks all pass.
Only ZZ- projects unless --allow-real-project (still never overwrites: every version is a new timeline).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plan import PlanError, load  # noqa: E402
from session import (PROJECT_COLOR, ResolveError, guard, open_project, project_color, require_zz,  # noqa: E402
                     resolve, resolve_turn, run_with_watchdog)

TAGS = {"ColorSpaceTag": "Rec.709", "GammaTag": "Rec.709-A"}    # 21.1.1: gives bt709/bt709/bt709 (QuickTime 1-1-1)
WANT_TAGS = {"color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709"}
MARK_COLOR = "Blue"


# ------------------------------------------------------------------------------------------------ Resolve side
def _import(mp, paths: list[str]) -> dict:
    """Import each path once (plain strings: the dict form returns []), keyed by file path."""
    have, stack = {}, [mp.GetRootFolder()]
    while stack:
        f = stack.pop()
        for c in f.GetClipList() or []:
            have[c.GetClipProperty("File Path")] = c
        stack += f.GetSubFolderList() or []
    need = [p for p in dict.fromkeys(paths) if p not in have]
    if need:
        for c in mp.ImportMedia(need) or []:
            have[c.GetClipProperty("File Path")] = c
    missing = [p for p in paths if p not in have]
    if missing:
        raise ResolveError(f"Resolve could not import: {missing[:5]}")
    return have


def build_timeline(project, ops: dict, name: str):
    mp = project.GetMediaPool()
    names = [project.GetTimelineByIndex(i + 1).GetName() for i in range(project.GetTimelineCount())]
    if name in names:
        raise ResolveError(f"timeline {name!r} already exists; every version is a new timeline (pick a new name)")
    items = _import(mp, [s["path"] for s in ops["sources"].values()])
    tl = mp.CreateEmptyTimeline(name)
    if not tl:
        raise ResolveError(f"could not create timeline {name!r}")
    project.SetCurrentTimeline(tl)
    tl = guard(tl)
    S = tl.GetStartFrame()             # 01:00:00:00 = frame 108000 at 30 fps; plan frames are offsets from here
    placed = {}

    def append(c, media_type):
        info = {"mediaPoolItem": items[ops["sources"][c["src"]]["path"]], "startFrame": c["s0"],
                "endFrame": c["s1"], "trackIndex": 1, "recordFrame": S + c["rec"], "mediaType": media_type}
        r = mp.AppendToTimeline([info])
        if not r:
            raise ResolveError(f"AppendToTimeline refused {c['id']} ({c['src']} {c['s0']}-{c['s1']} at {c['rec']})")
        return r[0]

    for c in sorted(ops["clips"], key=lambda c: c["rec"]):
        v = append(c, 1)
        if c["zoom"] != 1.0 and not v.SetProperties({"ZoomX": c["zoom"], "ZoomY": c["zoom"]}):
            raise ResolveError(f"SetProperties(zoom {c['zoom']}) refused for {c['id']}")
        v.AddMarker(0, MARK_COLOR, c["id"], "", 1, c["id"])
        placed[c["id"]] = v
        if c["audio"]:
            a = append(c, 2)
            tl.SetClipsLinked([v, a], True)
            placed[c["id"] + ".a"] = a
    done = []
    for t in ops["transitions"]:
        ok = placed[t["outgoing"]].AddTransition({"type": "Cross Dissolve", "category": "simple", "position": "end",
                                                  "alignment": "center", "duration": int(t["dur"])})
        if not ok:
            raise ResolveError(f"AddTransition into {t['into']} ({t['dur']}f) was refused")
        done.append(t["into"])
    return tl.unwrap(), S, done


def readback(tl, S) -> dict:
    out = {"video": [], "audio": []}
    for kind in out:
        for tr in range(1, tl.GetTrackCount(kind) + 1):
            for it in tl.GetItemListInTrack(kind, tr) or []:
                marks = (it.GetMarkers() or {}) if kind == "video" else {}
                out[kind].append({"track": tr, "name": it.GetName(), "start": it.GetStart() - S,
                                  "end": it.GetEnd() - S, "left_offset": it.GetLeftOffset(),
                                  "plan_id": next((m.get("customData") or m.get("name") for m in marks.values()),
                                                  None)})
    return out


def compare(ops: dict, rb: dict) -> list[str]:
    """Every planned clip must be on the timeline where the plan put it, from the planned source frame."""
    bad = []
    vids = {r["plan_id"]: r for r in rb["video"] if r["plan_id"]}
    auds = rb["audio"]
    for c in ops["clips"]:
        want = (c["rec"], c["rec"] + c["s1"] - c["s0"], c["s0"])
        r = vids.get(c["id"])
        if r is None:
            bad.append(f"{c['id']}: not found on the timeline")
            continue
        got = (r["start"], r["end"], r["left_offset"])
        if got != want:
            bad.append(f"{c['id']}: video at {got} (start, end, source in), plan {want}")
        if c["audio"] and not any((a["start"], a["end"], a["left_offset"]) == want for a in auds):
            bad.append(f"{c['id']}: no audio clip at {want}")
    return bad


def render(project, timeline, out_dir: str, name: str, ops: dict, timeout_s: int) -> str:
    R = resolve()
    project.SetCurrentTimeline(timeline)
    R.OpenPage("deliver")
    codecs = project.GetRenderCodecs("mp4") or {}
    h264 = next((v for k, v in codecs.items() if "H.264" in k and "265" not in k), "H264")
    if not project.SetCurrentRenderFormatAndCodec("mp4", h264):
        raise ResolveError("could not select mp4/H.264")
    out_dir = os.path.abspath(out_dir)            # a relative TargetDir: "Render Path Inaccessible", then a hang
    os.makedirs(out_dir, exist_ok=True)
    rs = {"TargetDir": out_dir, "CustomName": name, "SelectAllFrames": True, "ExportVideo": True, "ExportAudio": True,
          "FormatWidth": ops["size"][0], "FormatHeight": ops["size"][1], "FrameRate": ops["fps"],
          "AudioCodec": "aac", "AudioSampleRate": 48000, **TAGS}
    if not project.SetRenderSettings(rs):
        bad = [k for k, v in rs.items() if not project.SetRenderSettings({k: v})]
        raise ResolveError(f"render settings refused: {bad or rs}")
    jid = project.AddRenderJob()
    if not jid:
        raise ResolveError("AddRenderJob refused")
    try:
        project.StartRendering([jid])
        t0 = time.time()
        while project.IsRenderingInProgress():
            if time.time() - t0 > timeout_s:
                project.StopRendering()
                raise ResolveError(f"render did not finish in {timeout_s}s")
            time.sleep(1)
        st = project.GetRenderJobStatus(jid) or {}
        if st.get("JobStatus") != "Complete":
            raise ResolveError(f"render ended {st}")
    finally:
        project.DeleteRenderJob(jid)
        R.OpenPage("edit")
    out = os.path.join(out_dir, name + ".mp4")
    if not os.path.exists(out):
        raise ResolveError(f"render reported Complete but {out} is missing")
    return out


def child(a) -> dict:
    ops = load(a.edit)
    W, H = ops["size"]
    with resolve_turn(f"build {a.timeline}"):
        is_zz = a.project.startswith("ZZ-")
        p = open_project(a.project, create=is_zz, settings={
            "timelineResolutionWidth": W, "timelineResolutionHeight": H, "timelineFrameRate": ops["fps"],
            **PROJECT_COLOR})
        fps_now = float((p.GetSettings() or {}).get("timelineFrameRate") or 0)
        if abs(fps_now - ops["fps"]) > 0.01:
            raise ResolveError(f"{a.project} runs at {fps_now} fps, the plan at {ops['fps']}; use a new ZZ- project")
        tl, S, done = build_timeline(p, ops, a.timeline)
        rb = readback(tl, S)
        bad = compare(ops, rb)
        resolve().GetProjectManager().SaveProject()
        res = {"project": a.project, "timeline": a.timeline, "start_frame": S, "transitions": done,
               "warnings": ops["warnings"], "readback_ok": not bad, "readback_problems": bad,
               "readback": rb, "colour": project_color(p)}
        if bad or a.no_render:
            return res
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in a.timeline)
        res["render"] = render(p, tl, a.out, safe, ops, a.timeout)
    res["tags"] = output_tags(res["render"])
    res["tags_ok"] = res["tags"] == WANT_TAGS
    res["frames"] = frame_count(res["render"])
    res["frames_ok"] = abs(res["frames"] - ops["duration"]) <= 1
    return res


# ------------------------------------------------------------------------------------------------ file checks
def output_tags(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=color_primaries,color_transfer,color_space", "-of", "json", path],
                       capture_output=True, text=True)
    s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    return {k: s.get(k) for k in WANT_TAGS}


def frame_count(path: str) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", path], capture_output=True, text=True)
    return int((r.stdout.split() or ["0"])[0])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edit", help="edit file (plan.py's schema)")
    ap.add_argument("--project", required=True, help="a ZZ- project (created if missing)")
    ap.add_argument("--timeline", required=True, help="a NEW timeline name, e.g. 'EDIT v02 - tighter ending'")
    ap.add_argument("--out", required=True, help="render folder (made absolute)")
    ap.add_argument("--no-render", action="store_true", help="build and read back only")
    ap.add_argument("--allow-real-project", action="store_true", help="build into a non-ZZ project")
    ap.add_argument("--timeout", type=int, default=600, help="watchdog seconds for the whole Resolve job")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--result", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    a.out = os.path.abspath(a.out)
    if a.child:
        try:
            res = {"ok": False, **child(a)}
            res["ok"] = res["readback_ok"] and (a.no_render or (res["tags_ok"] and res["frames_ok"]))
        except (ResolveError, PlanError) as e:
            res = {"ok": False, "error": str(e)}
        except Exception as e:                       # an API surprise: report it instead of dying silently
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with open(a.result, "w") as f:
            json.dump(res, f, indent=1, default=str)
        return 0 if res["ok"] else 1
    try:
        ops = load(a.edit)                                   # fail fast, before Resolve
        if not a.allow_real_project:
            require_zz(a.project)
    except (PlanError, ResolveError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=1))
        return 1
    os.makedirs(a.out, exist_ok=True)
    result = os.path.join(a.out, f".build-result-{os.getpid()}.json")
    cmd = [sys.executable, os.path.abspath(__file__), *argv, "--child", "--result", result]
    try:
        code = run_with_watchdog(cmd, a.timeout)
    except ResolveError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=1))
        return 1
    try:
        with open(result) as f:
            res = json.load(f)
        os.remove(result)
    except (OSError, ValueError):
        res = {"ok": False, "error": f"the Resolve job exited {code} without a result"}
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
