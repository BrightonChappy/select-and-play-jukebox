"""Installs a newer version of the jukebox by itself when it starts.

Run by tray.py before the window opens, so it never cuts into music. It asks GitHub for the
newest release, downloads that release's files, checks they are complete and readable, keeps a
copy of the current version, then puts the new files in place. tray.py then starts the new copy.

Anything that goes wrong (no internet, GitHub down, a broken download) just leaves the current
version running. Your music, favourites and podcasts live in the data folder, which this never
touches. Turn it off with "Install updates automatically" on the Music folders page.
"""
import importlib.util
import io
import json
import os
import py_compile
import shutil
import tempfile
import time
import zipfile
from urllib.request import Request, urlopen

REPO = "BrightonChappy/select-and-play-jukebox"
NEEDED = ("server.py", "tray.py", "index.html", "version.json", "autoupdate.py")
UA = {"User-Agent": "SelectAndPlay-autoupdate"}


def _load(path, default):
    try:
        with open(path, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _ver(v):
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def _log(data_dir, text):
    try:
        with open(os.path.join(data_dir, "update-log.txt"), "a", encoding="utf8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def enabled(data_dir):
    st = _load(os.path.join(data_dir, "state.json"), {})
    return (st.get("settings") or {}).get("autoUpdate", True) is not False


def run(here, data_dir):
    """Returns True if a new version was put in place (the caller should restart)."""
    if os.environ.get("SELECTANDPLAY_NO_AUTOUPDATE") or not enabled(data_dir):
        return False
    cur = _load(os.path.join(here, "version.json"), {}).get("version", "0")
    try:
        with urlopen(Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                             headers={**UA, "Accept": "application/vnd.github+json"}), timeout=6) as r:
            rel = json.loads(r.read().decode("utf8"))
        tag = rel.get("tag_name") or ""
        latest = tag.lstrip("vV")
        if _ver(latest) <= _ver(cur):
            return False
        with urlopen(Request(f"https://github.com/{REPO}/archive/refs/tags/{tag}.zip", headers=UA), timeout=60) as r:
            blob = r.read()
    except Exception as e:  # offline, GitHub down, rate limited: try again next start
        _log(data_dir, f"check skipped ({e.__class__.__name__}: {e})")
        return False

    tmp = tempfile.mkdtemp(prefix="SelectAndPlayUpdate")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(tmp)
        tops = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        src = os.path.join(tmp, tops[0], "app") if len(tops) == 1 else ""
        if not src or not all(os.path.isfile(os.path.join(src, n)) for n in NEEDED):
            _log(data_dir, f"{latest}: download incomplete, not installed")
            return False
        newv = _load(os.path.join(src, "version.json"), {})
        if newv.get("version") != latest:
            _log(data_dir, f"{latest}: version file says {newv.get('version')}, not installed")
            return False
        # a version that needs an add-on this PC hasn't got is left for "Update now", which installs add-ons
        missing = [m for m in newv.get("modules", []) if importlib.util.find_spec(m) is None]
        if missing:
            _log(data_dir, f"{latest}: needs {', '.join(missing)}, left for Update now")
            return False
        for n in os.listdir(src):
            if n.endswith(".py"):
                py_compile.compile(os.path.join(src, n), cfile=os.path.join(tmp, n + "c"), doraise=True)

        # keep the current version, then copy the new one over it (put back if the copy fails)
        old = os.path.join(data_dir, "previous-version")
        shutil.rmtree(old, ignore_errors=True)
        shutil.copytree(here, old, ignore=shutil.ignore_patterns("__pycache__"))
        try:
            for n in os.listdir(src):
                s, d = os.path.join(src, n), os.path.join(here, n)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
        except Exception as e:
            for n in os.listdir(old):
                s, d = os.path.join(old, n), os.path.join(here, n)
                try:
                    shutil.copytree(s, d, dirs_exist_ok=True) if os.path.isdir(s) else shutil.copy2(s, d)
                except Exception:
                    pass
            _log(data_dir, f"{latest}: copy failed ({e}), old version put back")
            return False
        with open(os.path.join(data_dir, "just_updated.json"), "w", encoding="utf8") as f:
            json.dump({"from": cur, "to": latest, "notes": (rel.get("body") or "").strip()[:300]}, f)
        _log(data_dir, f"updated {cur} -> {latest}")
        return True
    except Exception as e:
        _log(data_dir, f"{latest}: not installed ({e.__class__.__name__}: {e})")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
