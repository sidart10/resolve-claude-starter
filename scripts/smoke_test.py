#!/usr/bin/env python3
"""End-to-end check against a live Resolve Studio, in the throwaway project ZZ-resolve-starter-smoke only.

    python3 smoke_test.py                                   plan + build + read-back + render + file checks
    python3 smoke_test.py --tree my-tree-v01.json           also applies your labelled tree and checks the labels

Makes three 12 s synthetic clips with ffmpeg (red and green with a tone, blue silent), plans an 8 s edit with a cut,
a full dissolve, a dissolve the checker has to shorten, and a 0.5 zoom, builds it as a new timeline, renders it and
measures the render: the colour at the centre of each shot, a blend in the middle of the dissolve, black corners on
the zoomed shot, bt709 tags and the frame count. Prints a JSON verdict; exit 1 on any failure.
Nothing outside the ZZ- project and the scratch folder is touched; your open project is reopened afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = "ZZ-resolve-starter-smoke"
COLORS = {"red": (208, 32, 32), "green": (32, 176, 64), "blue": (32, 64, 208)}


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def media(d: str) -> dict:
    os.makedirs(d, exist_ok=True)
    out = {}
    for i, (name, (r, g, b)) in enumerate(COLORS.items()):
        p = os.path.join(d, f"{name}.mov")
        audio = name != "blue"
        if not os.path.exists(p):
            src = ["-f", "lavfi", "-i", f"color=c=0x{r:02X}{g:02X}{b:02X}:s=1920x1080:r=30:d=12"]
            if audio:
                src += ["-f", "lavfi", "-i", f"sine=f={330 + 110 * i}:d=12:sample_rate=48000", "-shortest",
                        "-c:a", "pcm_s16le"]
            ff(*src, "-c:v", "prores_ks", "-profile:v", "1", p)
        out[name] = {"path": p, "duration": 12, "audio": audio}
    return out


def plan(src: dict) -> dict:
    return {"schema": "resolve-starter.edit.v1", "fps": 30, "size": [1920, 1080], "sources": src,
            "events": [{"id": "e1", "src": "red", "in": 1, "rec_in": 0, "dur": 3},
                       {"id": "e2", "src": "green", "in": 2, "rec_in": 3, "dur": 3,
                        "join_in": {"type": "dissolve", "dur": 1.0}},
                       {"id": "e3", "src": "blue", "in": 0.1, "rec_in": 6, "dur": 2, "zoom": 0.5,
                        "join_in": {"type": "dissolve", "dur": 1.0}}]}


def px(video: str, t: float, x: int, y: int) -> tuple:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", video, "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    i = (y * 1920 + x) * 3
    return tuple(raw[i:i + 3])


def near(c, rgb, tol=45) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c, rgb))


def run(cmd: list[str]) -> dict:
    r = subprocess.run([sys.executable, *cmd], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"ok": False, "error": (r.stdout + r.stderr)[-2000:]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.path.join(tempfile.gettempdir(), "resolve-starter-smoke"))
    ap.add_argument("--tree", help="a labelled tree file (see grade_by_label.py) to test colour by label too")
    a = ap.parse_args(argv)
    d = os.path.abspath(a.dir)
    edit_path = os.path.join(d, "edit.json")
    src = media(d)
    with open(edit_path, "w") as f:
        json.dump(plan(src), f, indent=1)
    name = "smoke " + time.strftime("%Y%m%d-%H%M%S")
    t0 = time.time()
    b = run([os.path.join(HERE, "build.py"), edit_path, "--project", PROJECT, "--timeline", name,
             "--out", os.path.join(d, "render")])
    verdict = {"ok": False, "timeline": name, "build_ok": b.get("ok"), "seconds": round(time.time() - t0, 1)}
    if not b.get("ok"):
        verdict["build"] = b
        print(json.dumps(verdict, indent=1))
        return 1
    out = b["render"]
    checks = {
        "readback_ok": b["readback_ok"],
        "tags": [b["tags"], b["tags_ok"]],
        "frames": [b["frames"], b["frames_ok"]],
        "dissolve_shortened_warning": any("shortened" in w for w in b["warnings"]),
        "transitions": b["transitions"],
        "red_at_1.5s": near(px(out, 1.5, 960, 540), COLORS["red"]),
        "mid_dissolve_blend": px(out, 3.0, 960, 540),
        "green_at_4.5s": near(px(out, 4.5, 960, 540), COLORS["green"]),
        "blue_centre_at_7s": near(px(out, 7.0, 960, 540), COLORS["blue"]),
        "zoomed_corner_black": sum(px(out, 7.0, 40, 40)) < 40,
    }
    blend = checks["mid_dissolve_blend"]
    checks["mid_dissolve_is_a_blend"] = 60 < blend[0] < 190 and 60 < blend[1] < 160
    ok = (checks["readback_ok"] and checks["tags"][1] and checks["frames"][1] and checks["dissolve_shortened_warning"]
          and checks["transitions"] == ["e2", "e3"] and checks["red_at_1.5s"] and checks["green_at_4.5s"]
          and checks["blue_centre_at_7s"] and checks["zoomed_corner_black"] and checks["mid_dissolve_is_a_blend"])
    if a.tree:
        with open(a.tree) as f:
            tree = json.load(f)
        toggles = [n["label"] for n in tree["nodes"] if "enabled" in n.get("scriptable", [])]
        writes_path = os.path.join(d, "writes.json")
        with open(writes_path, "w") as f:
            json.dump({"*": {toggles[-1]: {"enabled": False}}} if toggles else {}, f)
        g = run([os.path.join(HERE, "grade_by_label.py"), "apply", "--project", PROJECT, "--timeline", name,
                 "--tree", os.path.abspath(a.tree), "--writes", writes_path])
        checks["grade"] = {"ok": g.get("ok"), "error": g.get("error"),
                           "clips": [{k: c[k] for k in ("clip", "verified", "labels")} for c in g.get("clips", [])]}
        ok = ok and bool(g.get("ok"))
    verdict.update({"ok": ok, "render": out, "checks": checks, "seconds": round(time.time() - t0, 1)})
    print(json.dumps(verdict, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
