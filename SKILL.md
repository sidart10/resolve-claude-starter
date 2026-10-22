---
name: resolve-starter
description: Drive DaVinci Resolve Studio 21.1+ from Claude Code by script, safely. Use when asked to build or rebuild a timeline in Resolve from a plan, make a new version of an edit, render a timeline and check the file, apply a saved colour node tree and set its nodes by label, or try any Resolve scripting idea. Plans the cut in an edit file and checks it before Resolve opens, builds every version as a new timeline, reads every clip back, renders with Rec.709 tags and checks them, and grades only by node label. Works in ZZ- test projects first. Not for Fusion compositions, Fairlight mixing, or anything that needs keyframes on the Edit page.
---

# Resolve starter

Claude edits in Resolve by writing Python against Resolve's own scripting API. The official Resolve MCP (File >
Setup AI Assistants) is the front door: it is where you read the API and try things. The scripts in this folder are
where the edits you repeat end up.

## Before you write any Resolve code

Resolve changes faster than your training. The API stubs are the source of truth.

1. Call `get_whats_new` and `search_scripting_api` (Resolve MCP) for every call you plan to use. The changelog can
   lag the stubs; when they disagree, trust the stubs.
2. Quote the signatures you found.
3. Only then write the script, and run it in a ZZ- project first. A first script that only reads (project,
   timelines, colour settings) is the right first move.

## Resolve rules

These are enforced in `scripts/session.py`; follow them in any script you write by hand too.

- **ZZ- projects for anything new.** Never try a new script in a real project. `require_zz()` refuses other names;
  the scripts here need `--allow-real-project` to touch one.
- **A new timeline for every version** ("EDIT v07 - tighter ending"). Never overwrite or edit a built timeline in
  place: the user may have changed it by hand. `build.py` refuses an existing name.
- **Colour science on the project only, before any timeline exists.** Setting it on a timeline freezes Resolve
  21.1.1 (and the MCP with it). Plain DaVinci YRGB, not colour managed, so scripted CDLs and LUTs behave as written.
- **Never call `Insert*IntoTimeline`** (titles, generators, Fusion compositions). They ripple-insert at the playhead
  and split clips on every track. `guard(timeline)` blocks them. Render the piece outside Resolve as a transparent
  ProRes 4444 clip and place it on its own track instead.
- **Absolute render paths.** A relative TargetDir gives "Render Path Inaccessible" and the render hangs.
- **Save the open project and reopen it.** Wrap every Resolve job in `with resolve_turn("job"):`. It takes a lock
  (one job at a time), saves the project the user has open, and reopens it and its timeline afterwards.
- **Watchdog.** Run Resolve jobs in a child process under `run_with_watchdog`. If Resolve stops answering, the job is
  killed. Stop and tell the user; don't retry in a loop.
- **ImportMedia takes plain path strings.** The dict form returns `[]`.
- **AppendToTimeline's endFrame is exclusive.** Appending onto occupied space trims the new clip's head, so overlaps
  are a plan error, never something to let Resolve settle.
- **Timelines start at 01:00:00:00** (frame 108000 at 30 fps). Plan frames are offsets from `GetStartFrame()`.

## The loop

1. **Plan the cut in a file.** Write an edit file (`examples/edit.example.json`, schema in `scripts/plan.py`): sources
   with their durations, then events with `src`, `in`, `rec_in`, `dur`, optional `zoom` and `join_in` dissolve.
2. **Check it.** `python3 scripts/plan.py edit.json`. It refuses unknown sources, cuts outside a source, overlaps,
   and shortens dissolves that lack handles (with a warning). Fix the plan, not the timeline.
3. **Build a new timeline, read it back, render, check.**
   `python3 scripts/build.py edit.json --project ZZ-my-test --timeline "EDIT v01" --out /abs/render/dir`.
   It fails unless every clip reads back where the plan put it, the file reads `bt709,bt709,bt709` and the frame
   count matches. Gamma 2.4 tags look washed out on Macs and phones; Rec.709 / Rec.709-A is the pair that works.
4. **Show the user the render and let them watch it.** Their notes change the plan; the next build is a new version.

## Colour by label

`scripts/grade_by_label.py` applies a node tree the user built by hand and exported as a `.drx`, then reads the node
labels back and writes only CDL, LUT path and node on/off, only to labels the tree file marks scriptable.

- Never add, delete, reorder or relabel nodes. The API can't add nodes, and a script shouldn't.
- Never assume node 1. Indexes come from `GetNodeLabel` after the DRX is applied.
- After `ApplyGradeFromDRX`, fetch the graph again: the handle that applied it can read 0 nodes (21.1.1).
- Curves, windows, qualifiers and OFX are hand work.
- `SetLUT` only accepts paths relative to Resolve's LUT folder.

Run `python3 scripts/grade_by_label.py labels ...` first to see the labels a clip has.

## When the API can't do it

No keyframes on the Edit page, no volume automation, no subtitle styling. Make the piece in a tool that can
(HyperFrames, Remotion, ffmpeg), render it as a transparent ProRes 4444 clip, and place it on its own track. One
track per job. For music under speech, split the music into pieces at different levels with short crossfades.

## Growing this skill

When an edit works twice, add it here: a field in the plan, a check in `plan.py`, the placement in `build.py`, a
read-back comparison, a line in the smoke test. Run `python3 scripts/smoke_test.py` after every change.
