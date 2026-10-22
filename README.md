# Resolve starter: a Claude Code skill for DaVinci Resolve Studio

A small, working starting point for editing in DaVinci Resolve Studio with Claude Code. Claude plans the cut in a
file, a checker catches mistakes before Resolve opens, every version is built as a new timeline and read back clip by
clip, and every render is checked for the colour tags that keep it from looking washed out. Colour is set by node
label, never by node number.

It is a starter. It has one layout (one clip per shot, with an optional zoom), cuts and dissolves. Grow it with the
edits you repeat.

Built by Sid Dani ([@siddaniagi](https://x.com/siddaniagi)) from the setup he edits his own videos with.

## Quick start (about 5 minutes)

```bash
git clone https://github.com/sidart10/resolve-claude-starter ~/.claude/skills/resolve-starter
```

Then, with Resolve Studio open:

1. **File > Setup AI Assistants** in Resolve (one click).
2. Paste the [permissions](#permissions) into `~/.claude/settings.json`.
3. Run the smoke test: `python3 ~/.claude/skills/resolve-starter/scripts/smoke_test.py`. You want `"ok": true`.
4. Restart Claude Code and ask: *"Use the resolve-starter skill to list my Resolve project, timelines and colour settings."*

Or let Claude do the install: paste this into Claude Code.

```text
Install the Claude Code skill from https://github.com/sidart10/resolve-claude-starter into
~/.claude/skills/resolve-starter, add the permissions block from its README to ~/.claude/settings.json
(keep my existing settings), then tell me to open Resolve Studio and run its smoke test.
```

The details are below.

## What you need

- macOS with **DaVinci Resolve Studio 21.1 or newer**. The AI connection and Python scripting are Studio features.
- **Claude Code**, signed in.
- **Python 3.9+** and **ffmpeg** (with ffprobe): `brew install python ffmpeg`.
- In Resolve: Preferences > System > General > **External scripting using: Local**.

## Install

1. In Resolve: **File > Setup AI Assistants**. One click adds Resolve's own MCP server to Claude Code (and Claude
   Desktop). Check that Claude Code sees exactly one Resolve server:

   ```bash
   claude mcp list
   ```

2. Clone this repo into your skills, for all your projects:

   ```bash
   git clone https://github.com/sidart10/resolve-claude-starter ~/.claude/skills/resolve-starter
   ```

   or for one project only, into `<your project>/.claude/skills/resolve-starter`. Update later with
   `git -C ~/.claude/skills/resolve-starter pull`.

3. Add the permissions below to `~/.claude/settings.json`.

4. With Resolve open, run the smoke test. It works only in a throwaway project called `ZZ-resolve-starter-smoke`,
   uses synthetic clips it makes itself, and reopens whatever project you had open:

   ```bash
   python3 ~/.claude/skills/resolve-starter/scripts/smoke_test.py
   ```

   You want `"ok": true`. On a recent Mac it takes under ten seconds.

## Permissions

Reading is safe. Running code inside your editor isn't. The 8 read tools run without asking; running a script and
writing or deleting LUTs and DCTLs ask first.

```json
{
  "permissions": {
    "allow": [
      "mcp__DaVinci_Resolve__get_resolve_status",
      "mcp__DaVinci_Resolve__launch_resolve",
      "mcp__DaVinci_Resolve__get_whats_new",
      "mcp__DaVinci_Resolve__get_scripting_api",
      "mcp__DaVinci_Resolve__search_scripting_api",
      "mcp__DaVinci_Resolve__get_scripting_docs",
      "mcp__DaVinci_Resolve__list_luts",
      "mcp__DaVinci_Resolve__list_dctls"
    ],
    "ask": [
      "mcp__DaVinci_Resolve__run_script",
      "mcp__DaVinci_Resolve__run_script_unsafe",
      "mcp__DaVinci_Resolve__generate_lut",
      "mcp__DaVinci_Resolve__update_dctl",
      "mcp__DaVinci_Resolve__delete_lut",
      "mcp__DaVinci_Resolve__delete_dctl"
    ]
  }
}
```

The middle of each name must match your server's name from `claude mcp list`. If you run Claude Code inside the
Claude desktop app you may see a second copy named `DaVinci Resolve Studio`; write the same rules for
`mcp__DaVinci_Resolve_Studio__` too.

## Use it

Ask Claude for an edit in a ZZ- project, or run the steps yourself:

```bash
cd ~/.claude/skills/resolve-starter
python3 scripts/plan.py examples/edit.example.json            # check a plan (no Resolve needed)
python3 scripts/build.py my-edit.json --project ZZ-my-test \
  --timeline "EDIT v01" --out ~/Movies/renders/edit-v01       # new timeline, read-back, render, tag check
python3 scripts/grade_by_label.py labels --project ZZ-my-test --timeline "EDIT v01"
python3 scripts/grade_by_label.py apply --project ZZ-my-test --timeline "EDIT v01" \
  --tree my-tree-v01.json --writes my-writes.json             # apply your node tree, write by label
```

Source paths in an edit file may be relative to the file. Render folders are made absolute.

### Your node tree

`grade_by_label.py` needs a tree you built by hand once:

1. In a ZZ- project, open the Color page on a clip and build your nodes (for example: colour space in, balance,
   adjustments, look, texture, colour space out).
2. Label every node (right-click > Node Label). Labels are how scripts find nodes.
3. Right-click the viewer > Grab Still. In the Gallery, right-click the still > Export, and save it as
   `my-tree-v01.drx`.
4. Next to it, write `my-tree-v01.json` like `examples/tree.example.json`: the labels in order and what a script may
   write to each (`cdl`, `lut`, `enabled`). Colour space nodes stay empty: those are set by hand.

LUT paths in a writes file are relative to Resolve's LUT folder
(`/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT`). No LUTs ship with this skill.

## What's in here

```text
SKILL.md                    when Claude uses it, the Resolve rules, the loop
scripts/session.py          connect, ZZ- guard, lock + save/reopen, watchdog, Insert* block, project colour settings
scripts/plan.py             edit file -> checked frame-exact operations (no Resolve)
scripts/build.py            new timeline, read-back, render with Rec.709 tags, ffprobe check
scripts/grade_by_label.py   apply a .drx, re-fetch, check labels, write CDL / LUT / on-off by label
scripts/smoke_test.py       the end-to-end check in ZZ-resolve-starter-smoke
examples/                   an edit file, a tree file, a writes file
```

Each script has `--self-test` (plan.py, grade_by_label.py) or a docstring with its commands.

## Known limits (Resolve 21.1.1)

- No keyframes on the Edit page, no volume automation, no subtitle styling by script. Render those pieces outside
  Resolve as transparent ProRes 4444 clips and place them on their own tracks.
- The API can't add colour nodes. Build the tree by hand once.
- The MCP shares Resolve's one script server: when Resolve hangs, the MCP hangs. The watchdog kills the job; relaunch
  Resolve.

## License

MIT. See [LICENSE](LICENSE). Not affiliated with Blackmagic Design or Anthropic.
