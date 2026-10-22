"""Connect to DaVinci Resolve Studio (21.1+) from Python and keep your real projects safe.

Nothing connects at import time. What this module guards against (each one broke a real session on 21.1.1):
- Timeline.SetSettings with colorScienceMode never returns and freezes every script. Colour science is set on the
  PROJECT, before any timeline exists (set_project_settings); timeline_settings() refuses it.
- The Insert*IntoTimeline calls ripple-insert at the playhead and split clips on every track. guard() wraps a
  timeline so those calls raise instead of running.
- MediaPool.ImportMedia only accepts plain path strings; the dict form returns [].
- Resolve can stop answering. Run every Resolve job in a child process under run_with_watchdog so a hang is killed.
- Two scripts driving Resolve at once trample each other. resolve_turn holds a file lock, saves the project you have
  open, and reopens it (and its timeline) when the job ends.
- New things are tried in projects whose names start with ZZ- (require_zz), never in a real one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
LIB = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
LOCK_PATH = os.environ.get("RESOLVE_LOCK", "/tmp/resolve-build.lock")
FORBIDDEN = ("InsertTitleIntoTimeline", "InsertFusionTitleIntoTimeline", "InsertGeneratorIntoTimeline",
             "InsertFusionGeneratorIntoTimeline", "InsertFusionCompositionIntoTimeline", "InsertOFXGeneratorIntoTimeline")
# Plain DaVinci YRGB, not colour managed: LUTs and CDLs written by script behave as written. Set on the project only.
PROJECT_COLOR = {"colorScienceMode": "davinciYRGB"}

_resolve = None


class ResolveError(RuntimeError):
    pass


def resolve():
    """The Resolve app object. Raises with the fix when scripting is off or Resolve is not running."""
    global _resolve
    if _resolve is None:
        os.environ.setdefault("RESOLVE_SCRIPT_API", API)
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", LIB)
        mods = os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules")
        if mods not in sys.path:
            sys.path.append(mods)
        try:
            import DaVinciResolveScript as dvr  # noqa: E402
        except ImportError as e:
            raise ResolveError(f"cannot import DaVinciResolveScript from {mods} ({e}); is Resolve Studio installed?")
        _resolve = dvr.scriptapp("Resolve")
        if _resolve is None:
            raise ResolveError("DaVinci Resolve is not running or external scripting is off "
                               "(Preferences > System > General > External scripting using: Local)")
    return _resolve


def require_zz(name: str) -> str:
    if not name.startswith("ZZ-"):
        raise ResolveError(f"{name!r} is not a ZZ- test project; try new things in ZZ- projects only")
    return name


class guard:
    """Wrap a Timeline so the Insert*IntoTimeline calls raise instead of rippling every track."""

    def __init__(self, timeline):
        object.__setattr__(self, "_tl", timeline)

    def __getattr__(self, name):
        if name in FORBIDDEN:
            raise ResolveError(f"{name} ripples every track and splits clips; render the piece as a transparent "
                               "clip and place it on its own track instead")
        return getattr(self._tl, name)

    def unwrap(self):
        return self._tl


def open_project(name: str, create: bool = False, settings: dict | None = None):
    """Open project `name` from the current Project Manager folder (or the one already open). With create=True a
    missing project is made and `settings` (colour science included) are applied before any timeline exists."""
    pm = resolve().GetProjectManager()
    cur = pm.GetCurrentProject()
    if cur and cur.GetName() == name:
        return cur
    if name in (pm.GetProjectListInCurrentFolder() or []):
        p = pm.LoadProject(name)
        if not p:
            raise ResolveError(f"could not load project {name!r}")
        return p
    if not create:
        raise ResolveError(f"project {name!r} not found in the current Project Manager folder")
    p = pm.CreateProject(name)
    if not p:
        raise ResolveError(f"could not create project {name!r}")
    if settings:
        set_project_settings(p, settings)
    return p


def set_project_settings(project, settings: dict):
    """Project-level settings, one key at a time so a refusal names its key. Colour science only before timelines."""
    if "colorScienceMode" in settings and project.GetTimelineCount():
        cur = (project.GetSettings() or {}).get("colorScienceMode")
        if cur != settings["colorScienceMode"]:
            raise ResolveError("colorScienceMode must be set before the project has timelines "
                               f"(project has {project.GetTimelineCount()}, current {cur!r})")
    refused = [k for k, v in settings.items() if not project.SetSettings({k: str(v)})]
    if refused:
        cur = project.GetSettings() or {}
        raise ResolveError("project settings refused: " + ", ".join(
            f"{k}={settings[k]!r} (now {cur.get(k)!r})" for k in refused))


def timeline_settings(timeline, settings: dict):
    if "colorScienceMode" in settings or "useCustomSettings" in settings:
        raise ResolveError("timeline-level colour science hangs Resolve 21.1.1; set it on the project")
    if not timeline.SetSettings({k: str(v) for k, v in settings.items()}):
        raise ResolveError(f"timeline settings were refused: {settings}")


def project_color(project) -> dict:
    """Read-only: the project's colour science and whether Resolve colour management is on."""
    s = project.GetSettings() or {}
    return {k: s.get(k) for k in ("colorScienceMode", "isAutoColorManage", "colorSpaceTimeline", "colorSpaceOutput")}


def run_with_watchdog(cmd: list[str], timeout_s: int) -> int:
    """Run a Resolve job in a child process; kill it if it runs past timeout_s (Resolve has probably hung)."""
    proc = subprocess.Popen(cmd)
    t0 = time.time()
    while proc.poll() is None:
        if time.time() - t0 > timeout_s:
            proc.kill()
            raise ResolveError(f"the Resolve job did not finish in {timeout_s}s; Resolve may be hung. "
                               "Stop here, relaunch Resolve, and check what the job was doing.")
        time.sleep(1)
    return proc.returncode


def _savable(name: str | None) -> bool:
    """Save only named, real projects: SaveProject on 'Untitled Project' opens a name dialog that blocks Resolve.
    ZZ- projects are saved by the job that uses them."""
    return bool(name) and not name.startswith("Untitled Project") and not name.startswith("ZZ-")


class resolve_turn:
    """One Resolve job at a time: `with resolve_turn("my-job") as R:` takes an exclusive file lock, saves the project
    you have open, and reopens it and its timeline when the block ends (even on an error)."""

    def __init__(self, job: str, timeout_s: int = 3600):
        self.job, self.timeout_s, self.fh, self.back = job, timeout_s, None, None

    def __enter__(self):
        import fcntl
        self.fh = open(LOCK_PATH, "a+")
        t0 = time.time()
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() - t0 > self.timeout_s:
                    raise ResolveError(f"{self.job}: waited {self.timeout_s}s for another Resolve job ({LOCK_PATH})")
                time.sleep(2)
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"{self.job} {os.getpid()} {time.ctime()}\n")
        self.fh.flush()
        R = resolve()
        pm = R.GetProjectManager()
        cur = pm.GetCurrentProject()
        name = cur.GetName() if cur else None
        tl = cur.GetCurrentTimeline() if cur else None
        if _savable(name):
            pm.SaveProject()
        if _savable(name) or (name or "").startswith("ZZ-"):    # an unnamed scratch project: nothing to come back to
            self.back = {"project": name, "folder": pm.GetCurrentFolder(), "timeline": tl.GetName() if tl else None}
        return R

    def __exit__(self, *exc):
        import fcntl
        try:
            if self.back:
                reopen(self.back)
        finally:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False


def reopen(back: dict) -> None:
    """Reopen a project and its timeline by name, looking in its saved Project Manager folder, then the root."""
    pm = resolve().GetProjectManager()
    cur = pm.GetCurrentProject()
    if cur and cur.GetName() == back["project"]:
        p = cur
    else:
        if cur and cur.GetName().startswith("ZZ-"):
            pm.SaveProject()
        p = None
        for folder in (back.get("folder"), None):
            pm.GotoRootFolder()
            if folder and not pm.OpenFolder(folder):
                continue
            if back["project"] in (pm.GetProjectListInCurrentFolder() or []):
                p = pm.LoadProject(back["project"])
                break
        if not p:
            raise ResolveError(f"could not reopen {back['project']!r}; open it by hand")
    for i in range(1, p.GetTimelineCount() + 1):
        if p.GetTimelineByIndex(i).GetName() == back.get("timeline"):
            p.SetCurrentTimeline(p.GetTimelineByIndex(i))
            break
