"""Music folder jukebox: scans the parent folder and serves a web player.

Run:  python server.py            (this computer only)
      python server.py --lan      (also reachable from phones on your Wi-Fi)
"""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    import mutagen
except ImportError:
    sys.exit("Missing dependency. Run:  python -m pip install --user mutagen imageio-ffmpeg")

HERE = os.path.dirname(os.path.abspath(__file__))
MUSIC_ROOT = ""  # tracks hold full paths, so joining with "" leaves them as they are
PORT = int(os.environ.get("SELECTANDPLAY_PORT") or 8765)
# everything personal (favourites, podcasts, settings, caches) lives here, so a new
# version of the program can be dropped over the old one without losing anything
DATA_DIR = os.environ.get("SELECTANDPLAY_DATA") or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SelectAndPlay")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DEFAULT_NAME = "Select & Play"
AUDIO_EXT = {".mp3": 0, ".m4a": 1, ".flac": 2, ".ogg": 3, ".wav": 4, ".wma": 9}  # lower = preferred
BROWSER_OK = {".mp3", ".m4a", ".flac", ".ogg", ".wav"}
CACHE_DIR = os.path.join(DATA_DIR, "cache")
INDEX_FILE = os.path.join(DATA_DIR, "library_cache.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MOODS_FILE = os.path.join(HERE, "moods.json")

state_lock = threading.Lock()
library = {"albums": [], "tracks": []}
track_by_id = {}
album_by_id = {}
transcode_locks = {}
transcode_lock_guard = threading.Lock()


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()


def sid(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- tags

def _first(v):
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None:
        return None
    v = getattr(v, "value", v)
    s = str(v).strip()
    return s or None


def read_tags(path):
    """Returns dict with title, artist, album, genre, year, track, duration, lyrics, has_art."""
    out = {}
    try:
        m = mutagen.File(path)
    except Exception:
        return out
    if m is None:
        return out
    out["duration"] = round(getattr(m.info, "length", 0) or 0, 1)
    t = m.tags
    if t is None:
        return out
    cls = type(m).__name__
    if cls == "ASF":
        g = lambda k: _first(t.get(k))
        out.update(title=g("Title"), artist=g("Author"), albumartist=g("WM/AlbumArtist"),
                   album=g("WM/AlbumTitle"), genre=g("WM/Genre"), year=g("WM/Year"),
                   track=g("WM/TrackNumber"), lyrics=g("WM/Lyrics"),
                   has_art=bool(t.get("WM/Picture")))
    elif cls in ("MP4", "EasyMP4"):
        g = lambda k: _first(t.get(k))
        trk = t.get("trkn")
        out.update(title=g("\xa9nam"), artist=g("\xa9ART"), albumartist=g("aART"), album=g("\xa9alb"),
                   genre=g("\xa9gen"), year=g("\xa9day"), lyrics=g("\xa9lyr"),
                   track=str(trk[0][0]) if trk else None, has_art=bool(t.get("covr")))
    elif cls in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC"):
        g = lambda k: _first(t.get(k))
        out.update(title=g("title"), artist=g("artist"), albumartist=g("albumartist"), album=g("album"),
                   genre=g("genre"), year=g("date") or g("year"), track=g("tracknumber"),
                   lyrics=g("lyrics") or g("unsyncedlyrics"), has_art=bool(getattr(m, "pictures", None)))
    else:  # ID3 and friends
        g = lambda k: _first(t.get(k))
        lyr = None
        for k in t.keys():
            if k.startswith("USLT"):
                lyr = getattr(t[k], "text", None)
                break
        out.update(title=g("TIT2"), artist=g("TPE1"), albumartist=g("TPE2"), album=g("TALB"), genre=g("TCON"),
                   year=g("TDRC") or g("TYER"), track=g("TRCK"), lyrics=lyr,
                   has_art=any(k.startswith("APIC") for k in t.keys()))
    if out.get("year"):
        y = re.match(r"\d{4}", out["year"])
        out["year"] = int(y.group()) if y else None
        if out["year"] and out["year"] < 1950:  # e.g. opera composition dates
            out["year"] = None
    if out.get("track"):
        n = re.match(r"\d+", out["track"])
        out["track"] = int(n.group()) if n else None
    return out


def embedded_art(path):
    try:
        m = mutagen.File(path)
        t = m.tags
        cls = type(m).__name__
        if cls == "ASF":
            pics = t.get("WM/Picture")
            if pics:
                data = pics[0].value
                # ASF picture: mime(utf16z) type(1) size(4)... parse loosely by finding JPEG/PNG magic
                for magic, mime in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png")):
                    i = data.find(magic)
                    if i >= 0:
                        return data[i:], mime
        elif cls == "MP4":
            covr = t.get("covr")
            if covr:
                c = covr[0]
                return bytes(c), "image/png" if c.imageformat == 14 else "image/jpeg"
        else:
            for k in t.keys():
                if k.startswith("APIC"):
                    return t[k].data, t[k].mime or "image/jpeg"
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------- scanning

def norm_title(name):
    name = os.path.splitext(name)[0]
    name = re.sub(r"^\d+[\s._-]*", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def clean_title(name):
    name = os.path.splitext(name)[0]
    name = re.sub(r"^\d+[\s._-]+", "", name)
    return name.rstrip("-").strip()


def find_cover(dirpath, files):
    lower = {f.lower(): f for f in files}
    if "folder.jpg" in lower:
        return lower["folder.jpg"]
    large = [f for f in files if f.lower().startswith("albumart") and f.lower().endswith("_large.jpg")]
    if large:
        return large[0]
    for f in files:
        if f.lower() in ("cover.jpg", "cover.png", "front.jpg", "albumartsmall.jpg"):
            return f
    return None


def settings():
    return load_json(SETTINGS_FILE, {})


def music_folders(sources=False):
    """The folders chosen on the setup screen, minus any that sit inside another one.
    With copying on, the jukebox plays from its own folder; sources=True gives the folders it copies from."""
    st = settings()
    if st.get("copyTo") and not sources:
        os.makedirs(st["copyTo"], exist_ok=True)
        return [os.path.normpath(st["copyTo"])]
    fs = [os.path.normpath(f) for f in st.get("folders", []) if os.path.isdir(f)]
    fs.sort(key=len)
    out = []
    for f in fs:
        if not any(f.lower() == o.lower() or f.lower().startswith(o.lower() + os.sep) for o in out):
            out.append(f)
    return out


def _walk_all(sources=False):
    for root in music_folders(sources):
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(("_", ".", "$")) and d.lower() not in SKIP_DIRS)
            yield root, dirpath, files


SKIP_DIRS = {"node_modules", "appdata", "windows", "program files", "program files (x86)", "programdata", "system volume information", "recycler"}


def _most(vals):
    vals = [v.strip() for v in vals if v and v.strip()]
    return max(set(vals), key=vals.count) if vals else None


def scan():
    cache = load_json(INDEX_FILE, {})
    new_cache = {}
    albums, tracks = [], []
    for root, dirpath, files in _walk_all():
        audio = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT]
        if not audio:
            continue
        rel_dir = dirpath.replace("\\", "/")
        # artist folder above the album folder (Artist/Album layout), if there is one
        inside = os.path.relpath(dirpath, root).replace("\\", "/").split("/")
        top = inside[-2] if len(inside) >= 2 and inside[0] != "." else None
        if top and re.search(r"disc|cd\s*\d", inside[-1], re.I) and len(inside) >= 3:
            top = inside[-3]
        # de-duplicate the same song stored in several formats
        best = {}
        for f in audio:
            ext = os.path.splitext(f)[1].lower()
            key = norm_title(f)
            num = re.match(r"\d+", f)
            key = (num.group().lstrip("0") if num else "") + "|" + key
            if key not in best or AUDIO_EXT[ext] < AUDIO_EXT[os.path.splitext(best[key])[1].lower()]:
                best[key] = f
        album_id = sid(rel_dir)
        album_tracks = []
        for f in sorted(best.values()):
            full = os.path.join(dirpath, f)
            rel = rel_dir + "/" + f
            st = os.stat(full)
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            c = cache.get(rel)
            if c and c.get("sig") == sig:
                tags = c["tags"]
            else:
                tags = read_tags(full)
                tags["has_lyrics"] = bool(tags.pop("lyrics", None))
            new_cache[rel] = {"sig": sig, "tags": tags}
            ext = os.path.splitext(f)[1].lower()
            num = re.match(r"\d+", f)
            artist = tags.get("artist") or (top if top and top != "Various Artists" else None)
            title = tags.get("title") or clean_title(f)
            if not tags.get("artist") and " - " in clean_title(f):
                artist, title = clean_title(f).split(" - ", 1)
            album_tracks.append({
                "id": sid(rel), "album": album_id, "path": rel,
                "title": title, "artist": artist or "Various Artists",
                "n": tags.get("track") or (int(num.group()) if num else None),
                "dur": tags.get("duration") or 0, "fmt": ext[1:],
                "needsConvert": ext not in BROWSER_OK,
                "hasLyrics": bool(tags.get("has_lyrics")),
                "_genre": tags.get("genre"), "_year": tags.get("year"),
                "_aa": tags.get("albumartist"), "_alb": tags.get("album"),
                "_hasArt": tags.get("has_art"),
            })
        album_tracks.sort(key=lambda t: (t["n"] or 999, t["title"]))
        folder = rel_dir.split("/")[-1]
        disc = re.search(r"\bDisc (\d+)\s*$", folder, re.I)
        genres = [t["_genre"] for t in album_tracks if t["_genre"]]
        years = [t["_year"] for t in album_tracks if t["_year"]]
        cover = find_cover(dirpath, files)
        art_src = next((t["path"] for t in album_tracks if t["_hasArt"]), None)
        artists = {t["artist"] for t in album_tracks}
        album_artist = (_most([t["_aa"] for t in album_tracks])
                        or (artists.pop() if len(artists) == 1 else None)
                        or (top if top and top != "Various Artists" else "Various Artists"))
        tag_album = _most([t["_alb"] for t in album_tracks])
        tag_disc = re.search(r"[\[(]?\b(?:disc|disk|cd)\s*(\d+)\b[\])]?", tag_album or "", re.I)
        if tag_album:
            tag_album = re.sub(r"\s*[\[(]?\b(?:disc|disk|cd)\s*\d+\b[\])]?\s*$", "", tag_album, flags=re.I).strip(" -:") or tag_album
        albums.append({
            "id": album_id, "path": rel_dir, "artist": album_artist,
            "title": tag_album or (re.sub(r"\s*Disc \d+\s*$", "", folder, flags=re.I).strip() or folder).replace("_ ", ": "),
            "folder": folder, "disc": int(disc.group(1)) if disc else int(tag_disc.group(1)) if tag_disc else None,
            "tagGenre": max(set(genres), key=genres.count).strip() if genres else None,
            "year": max(set(years), key=years.count) if years else None,
            "hasArt": bool(cover or art_src),
            "_cover": cover, "_artSrc": art_src,
            "tracks": [t["id"] for t in album_tracks],
            "duration": round(sum(t["dur"] for t in album_tracks)),
            "added": int(os.path.getmtime(dirpath)),
        })
        for t in album_tracks:
            for k in ("_genre", "_year", "_hasArt", "_aa", "_alb"):
                t.pop(k)
        tracks.extend(album_tracks)
    save_json(INDEX_FILE, new_cache)
    albums, merged = merge_discs(albums)
    for t in tracks:
        t["album"] = merged.get(t["id"], t["album"])
    # the same album in two ticked folders (a backup copy, an old OneDrive folder...) shows once
    # (keeps whichever copy has the most songs)
    best = {}
    for a in albums:
        k = (a["artist"].lower(), a["title"].lower())
        if k not in best or len(a["tracks"]) > len(best[k]["tracks"]):
            best[k] = a
    keep_ids = {id(a) for a in best.values()}
    dropped = {t for a in albums if id(a) not in keep_ids for t in a["tracks"]}
    albums = [a for a in albums if id(a) in keep_ids]
    tracks = [t for t in tracks if t["id"] not in dropped]
    apply_curation(albums)
    library["albums"], library["tracks"] = albums, tracks
    track_by_id.clear()
    track_by_id.update({t["id"]: t for t in tracks})
    album_by_id.clear()
    album_by_id.update({a["id"]: a for a in albums})
    print(f"Library: {len(albums)} albums, {len(tracks)} tracks")


def merge_discs(albums):
    """Disc 1 / Disc 2 folders (and bonus editions like "[UK]") of one album become one record.
    The first disc keeps its id, so its moods and history carry over; songs keep their own ids."""
    parent = lambda a: a["path"].rsplit("/", 2)[0] if re.search(r"\b(disc|disk|cd)\s*\d+\s*$", a["folder"], re.I) else a["path"].rsplit("/", 1)[0]
    base = lambda a: (parent(a).lower(), a["artist"].lower(), re.sub(r"\s*\[[^\]]*\]\s*", " ", a["title"]).strip().lower())
    groups = {}
    for a in albums:
        groups.setdefault(base(a), []).append(a)
    out, merged = [], {}
    for a in albums:
        g = groups[base(a)]
        if len(g) == 1:
            out.append(a)
            continue
        if a is not g[0]:
            continue
        g.sort(key=lambda x: (x["disc"] or 0, "[" in x["folder"], x["folder"]))
        first = g[0]
        m = dict(first)
        m["title"] = re.sub(r"\s*\[[^\]]*\]\s*", " ", first["title"]).strip()
        m["disc"] = None
        m["discs"] = len(g)
        m["tracks"] = [t for x in g for t in x["tracks"]]
        m["duration"] = sum(x["duration"] for x in g)
        art = next((x for x in g if x["hasArt"]), first)
        m["hasArt"], m["_cover"], m["_artSrc"] = art["hasArt"], art["_cover"], art["_artSrc"]
        m["_coverDir"] = art["path"]
        m["year"] = next((x["year"] for x in g if x["year"]), None)
        m["tagGenre"] = next((x["tagGenre"] for x in g if x["tagGenre"]), None)
        merged.update({t: first["id"] for t in m["tracks"]})
        out.append(m)
    return out, merged


GENRE_DEFAULT_MOODS = {
    "Classical": ["Dramatic", "Focus"], "Opera": ["Dramatic", "Romantic"],
    "Dance": ["High Energy", "Party"], "Electronica": ["Chill"], "Pop": ["Feel-Good", "Sing-Along"],
    "Rock": ["Road Trip", "High Energy"], "Alternative": ["Reflective"], "Indie": ["Reflective", "Road Trip"],
    "Soundtrack": ["Dramatic"], "Soul": ["Romantic", "Late Night"], "R&B": ["Romantic", "Late Night"],
    "Hip-Hop": ["Workout", "Party"], "Jazz": ["Late Night", "Chill"], "Blues": ["Late Night", "Heartbreak"],
    "Country": ["Road Trip", "Heartbreak"], "Folk": ["Reflective", "Chill"], "Metal": ["Workout", "Rebellious"],
    "Punk": ["Rebellious", "High Energy"], "Reggae": ["Chill", "Feel-Good"], "Easy Listening": ["Chill", "Nostalgic"],
    "Musicals": ["Sing-Along", "Dramatic"], "Ambient": ["Focus", "Chill"], "Disco": ["Party", "Feel-Good"],
    "Oldies": ["Nostalgic", "Sing-Along"], "Latin": ["Party", "Feel-Good"], "World": ["Uplifting"],
    "Gospel": ["Uplifting"], "Christmas": ["Feel-Good", "Nostalgic"], "Spoken Word": [],
}
GENRE_WORDS = [  # tag text -> tidy genre (first match wins)
    ("christmas|holiday", "Christmas"), ("opera", "Opera"), ("classical|baroque|symphon|orchestr|concerto", "Classical"),
    (r"soundtrack|score|film|\bost\b|o\.s\.t", "Soundtrack"), ("musical|broadway|show tunes", "Musicals"),
    (r"hip.?hop|\brap\b|grime|drill|\btrap\b", "Hip-Hop"), ("r&b|rnb|r ?& ?b|rhythm and blues", "R&B"), ("soul|motown|funk", "Soul"),
    ("disco", "Disco"), (r"house|techno|trance|\bedm\b|dance|garage|drum|dubstep", "Dance"), (r"electro|synth|\bidm\b", "Electronica"),
    ("ambient|new age|meditat", "Ambient"), ("metal|hardcore", "Metal"), ("punk", "Punk"), ("indie", "Indie"),
    (r"alternative|\balt\b|grunge|britpop", "Alternative"), ("rock", "Rock"), ("jazz|swing|bebop", "Jazz"), ("blues", "Blues"),
    ("country|americana|bluegrass", "Country"), ("folk|celtic|acoustic|singer", "Folk"), (r"reggae|\bska\b|\bdub\b", "Reggae"),
    ("latin|salsa|samba|reggaeton", "Latin"), ("gospel|christian|worship", "Gospel"), ("easy|lounge|crooner|vocal", "Easy Listening"),
    ("oldies|50s|60s|rock.?n.?roll", "Oldies"), ("world|afro", "World"), ("audiobook|spoken|speech|comedy", "Spoken Word"), ("pop", "Pop"),
]


def tidy_genre(g):
    if not g:
        return None
    g = re.sub(r"^\(\d+\)", "", g).strip()  # old ID3 "(13)Pop"
    low = g.lower()
    for pat, name in GENRE_WORDS:
        if re.search(pat, low):
            return name
    return g.title()[:24] or None


def apply_curation(albums):
    cur = load_json(MOODS_FILE, {"moods": {}, "albums": {}})
    user = load_json(STATE_FILE, {}).get("albumMoods", {})
    for a in albums:
        c = cur["albums"].get(a["path"], {})
        a["genre"] = c.get("genre") or tidy_genre(a["tagGenre"]) or "Other"
        a["year"] = c.get("year") or a["year"]
        a["moods"] = user.get(a["id"]) if a["id"] in user else (
            c.get("moods") or GENRE_DEFAULT_MOODS.get(a["genre"], []))
    library["moodDefs"] = cur["moods"]


# ---------------------------------------------------------------- copy everything into one jukebox folder
_bad = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
safe_name = lambda t: (_bad.sub("_", t or "").strip(" .") or "Unknown")[:80]
sync_state = {"running": False, "copied": 0, "toCopy": 0, "last": 0, "lastAdded": 0, "error": "", "dest": ""}
sync_lock = threading.Lock()


def default_copy_folder():
    known = _known_music()
    return os.path.join(known[0] if known else os.path.join(os.path.expanduser("~"), "Music"), "Select & Play Library")


def sync_copy():
    """Copies songs from the chosen folders into the jukebox folder as Artist/Album/song.
    Only new songs are copied (same name and size = already there). Nothing is ever deleted or moved."""
    st = settings()
    dest = st.get("copyTo")
    if not dest or not sync_lock.acquire(blocking=False):
        return 0
    try:
        sync_state.update(running=True, copied=0, toCopy=0, error="", dest=dest)
        jobs = []
        dest_l = os.path.normpath(dest).lower()
        for root, dirpath, files in _walk_all(sources=True):
            if os.path.normpath(dirpath).lower().startswith(dest_l):
                continue  # never copy the jukebox folder into itself
            audio = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT]
            if not audio:
                continue
            tags = [read_tags(os.path.join(dirpath, f)) for f in audio]
            artist = _most([t.get("albumartist") for t in tags]) or _most([t.get("artist") for t in tags])
            album = _most([t.get("album") for t in tags])
            if not artist or not album:  # fall back to the folders it was in
                parts = os.path.relpath(dirpath, root).split(os.sep)
                album = album or (parts[-1] if parts[-1] != "." else "Singles")
                artist = artist or (parts[-2] if len(parts) >= 2 else "Unknown Artist")
            out_dir = os.path.join(dest, safe_name(artist), safe_name(album))
            for f in audio:
                src, dst = os.path.join(dirpath, f), os.path.join(out_dir, f)
                try:
                    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                        continue
                except OSError:
                    continue
                jobs.append((src, dst))
            # bring the cover picture along too
            for f in files:
                if f.lower() in ("folder.jpg", "cover.jpg", "front.jpg", "albumart.jpg", "folder.png", "cover.png"):
                    if not os.path.exists(os.path.join(out_dir, f)):
                        jobs.append((os.path.join(dirpath, f), os.path.join(out_dir, f)))
        need = sum(os.path.getsize(s) for s, _ in jobs if os.path.exists(s))
        free = shutil.disk_usage(os.path.splitdrive(os.path.abspath(dest))[0] + os.sep).free
        if need > free - 500 * 1024 * 1024:
            sync_state["error"] = f"Not enough space: copying needs {need / 1e9:.1f} GB, the drive has {free / 1e9:.1f} GB free."
            return 0
        sync_state["toCopy"] = len(jobs)
        for src, dst in jobs:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                sync_state["copied"] += 1
            except OSError as e:
                sync_state["error"] = f"Couldn't copy {os.path.basename(src)} ({e.strerror or e})"
        if sync_state["copied"]:
            scan()
            sync_state["lastAdded"] = time.time()
        return sync_state["copied"]
    finally:
        sync_state.update(running=False, last=time.time())
        sync_lock.release()


def sync_loop():
    """Copies new songs when the jukebox starts and every 30 minutes after that."""
    while True:
        try:
            sync_copy()
        except Exception as e:  # keep the loop alive whatever happens
            sync_state["error"] = str(e)
        time.sleep(30 * 60)


# ---------------------------------------------------------------- updates: is there a newer release on GitHub?
REPO = "BrightonChappy/select-and-play-jukebox"
update_cache = {}


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def local_version():
    return load_json(os.path.join(HERE, "version.json"), {}).get("version", "0")


def check_update():
    """Asks GitHub for the newest release (at most every 6 hours). Never raises."""
    hit = update_cache.get("r")
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    cur = local_version()
    res = {"current": cur, "latest": cur, "newer": False, "notes": ""}
    try:
        req = Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                      headers={"User-Agent": "SelectAndPlay-update-check", "Accept": "application/vnd.github+json"})
        with urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf8"))
        latest = (d.get("tag_name") or "").lstrip("vV")
        res.update(latest=latest or cur, newer=_ver(latest) > _ver(cur),
                   notes=_text(d.get("body") or "").strip()[:300])
    except (HTTPError, URLError, OSError, ValueError):
        pass
    update_cache["r"] = (time.time(), res)
    return res


def run_update():
    """Starts the installer in its own window. It downloads the new version, closes this
    jukebox, puts the new files in place and opens it again. Music and favourites are kept."""
    script = os.path.join(HERE, "install.ps1")
    if not os.path.exists(script):
        return False
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Update"],
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)
    return True


# ---------------------------------------------------------------- first-run setup: where is the music?
def _known_music():
    """Windows' own Music folder (follows OneDrive or a moved folder), plus the other usual places."""
    out = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            for name in ("My Music", "{1CF1260C-4DD0-4ebb-811F-33C572699FDE}"):
                try:
                    out.append(os.path.expandvars(winreg.QueryValueEx(k, name)[0]))
                except OSError:
                    pass
    except ImportError:
        pass
    home = os.path.expanduser("~")
    out += [os.path.join(home, "Music"), os.path.join(home, "Downloads"), os.path.join(home, "Desktop"),
            os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Music")]
    try:
        for d in os.listdir(home):
            if d.lower().startswith("onedrive"):
                out += [os.path.join(home, d, "Music"), os.path.join(home, d, "Desktop", "Music")]
    except OSError:
        pass
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive = letter + ":\\"
        if os.path.exists(drive):
            out += [os.path.join(drive, n) for n in ("Music", "My Music", "MP3", "MP3s", "Media", "iTunes")]
    seen, res = set(), []
    for f in out:
        f = os.path.normpath(f)
        if f.lower() not in seen and os.path.isdir(f):
            seen.add(f.lower())
            res.append(f)
    return res


def count_audio(folder, limit_s=4.0):
    """Songs in a folder and below (stops looking after a few seconds on huge drives)."""
    n, end, sample = 0, time.time() + limit_s, ""
    for dirpath, dirnames, files in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(("_", ".", "$")) and d.lower() not in SKIP_DIRS]
        hits = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT]
        if hits and not sample:
            sample = os.path.relpath(dirpath, folder)
        n += len(hits)
        if time.time() > end:
            return n, sample, True
    return n, sample, False


found_cache = {}


def find_music():
    if "list" in found_cache and time.time() - found_cache["t"] < 120:
        return found_cache["list"]
    res = []
    for f in _known_music():
        n, sample, more = count_audio(f)
        if n:
            res.append({"path": f, "songs": n, "more": more, "sample": "" if sample == "." else sample})
    found_cache.update(list=res, t=time.time())
    return res


def pick_folder():
    """Opens Windows' folder picker in a separate little process and returns the folder (or "")."""
    code = ("import tkinter as tk; from tkinter import filedialog; r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "print(filedialog.askdirectory(title='Choose a folder with music in it') or '')")
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    try:
        out = subprocess.run([exe, "-c", code], capture_output=True, text=True, timeout=600,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return os.path.normpath(out.stdout.strip()) if out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- transcoding

def cached_mp3(track):
    return os.path.join(CACHE_DIR, track["id"] + ".mp3")


def ensure_converted(track):
    out = cached_mp3(track)
    if os.path.exists(out):
        return out
    if not FFMPEG:
        return None
    with transcode_lock_guard:
        lock = transcode_locks.setdefault(track["id"], threading.Lock())
    with lock:
        if os.path.exists(out):
            return out
        os.makedirs(CACHE_DIR, exist_ok=True)
        src = os.path.join(MUSIC_ROOT, track["path"])
        tmp = out + ".part"
        r = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", src, "-vn", "-map_metadata", "-1",
                            "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", tmp],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not os.path.exists(tmp):
            print("Convert failed:", track["path"], r.stderr.decode(errors="ignore")[:300])
            return None
        os.replace(tmp, out)
        return out


def prewarm():
    todo = [t for t in library["tracks"] if t["needsConvert"] and not os.path.exists(cached_mp3(t))]
    if todo:
        print(f"Converting {len(todo)} WMA tracks in the background (first run only)...")
    for i, t in enumerate(todo, 1):
        ensure_converted(t)
        if i % 25 == 0:
            print(f"  converted {i}/{len(todo)}")
    if todo:
        print("Background conversion finished.")


# ---------------------------------------------------------------- internet (Internet Archive)

IA = "https://archive.org"
UA = {"User-Agent": "SelectAndPlay-Jukebox/1.0 (personal use)"}
NOT_MUSIC = "-collection:(podcasts OR librivoxaudio OR audio_bookspoetry OR oldtimeradio OR radioprograms OR audio_podcast OR audio_religion OR audio_news)"
NET_WHERE = {
    "music": f"mediatype:(audio OR etree) AND {NOT_MUSIC}",
    "live": "mediatype:(etree)",
    "78s": "mediatype:(audio) AND collection:(78rpm)",
    "netlabels": "mediatype:(audio) AND collection:(netlabels)",
    "all": "mediatype:(audio OR etree)",
}
NET_FORMATS = ["VBR MP3", "128Kbps MP3", "MP3", "64Kbps MP3", "Ogg Vorbis", "Flac"]  # first = preferred
net_cache = {}  # identifier -> parsed item


def ia_get(url, params=None, timeout=20):
    if params:
        url += "?" + urlencode(params, doseq=True)
    with urlopen(Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _text(v):
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v[:3])
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(v or "")).replace("_", " ")).strip()


def net_search(q, where="music", page=1):
    words = re.findall(r"[\w']+", q.lower())[:8]
    if not words:
        return {"total": 0, "items": []}
    w = " AND ".join(words)
    query = f"(title:({w}) OR creator:({w}) OR subject:({w})) AND {NET_WHERE.get(where, NET_WHERE['music'])}"
    d = ia_get(IA + "/advancedsearch.php", {
        "q": query, "fl[]": ["identifier", "title", "creator", "year", "date", "downloads", "mediatype"],
        "sort[]": "downloads desc", "rows": 48, "page": max(1, page), "output": "json"})["response"]
    items = []
    for x in d["docs"]:
        year = x.get("year") or str(x.get("date") or "")[:4]
        items.append({"id": x["identifier"], "title": _text(x.get("title")) or x["identifier"],
                      "artist": _text(x.get("creator")), "year": int(year) if str(year).isdigit() else None,
                      "downloads": x.get("downloads") or 0, "live": x.get("mediatype") == "etree"})
    return {"total": d["numFound"], "items": items}


def _secs(v):
    try:
        if ":" in str(v):
            p = [float(x) for x in str(v).split(":")]
            return sum(x * 60 ** i for i, x in enumerate(reversed(p)))
        return float(v)
    except (TypeError, ValueError):
        return 0


def net_item(ident):
    if ident in net_cache:
        return net_cache[ident]
    d = ia_get(f"{IA}/metadata/{quote(ident)}")
    md = d.get("metadata") or {}
    if not md:
        return None
    # one file per recording: the best playable copy of each original
    best = {}
    for f in d.get("files", []):
        fmt = f.get("format", "")
        if fmt not in NET_FORMATS or f.get("private") == "true":
            continue
        key = f.get("original") if f.get("source") == "derivative" else f["name"]
        key = os.path.splitext(key or f["name"])[0]
        if key not in best or NET_FORMATS.index(fmt) < NET_FORMATS.index(best[key].get("format")):
            best[key] = f
    # titles and lengths often live on the original file only
    orig = {os.path.splitext(f["name"])[0]: f for f in d.get("files", []) if f.get("source") == "original"}
    tracks = []
    for key, f in best.items():
        o = orig.get(key, {})
        title = f.get("title") or o.get("title") or clean_title(os.path.basename(f["name"]))
        n = re.match(r"\d+", str(f.get("track") or o.get("track") or ""))
        tracks.append({"file": f["name"], "title": _text(title), "artist": _text(f.get("creator") or o.get("creator") or md.get("creator")),
                       "n": int(n.group()) if n else None, "dur": round(_secs(f.get("length") or o.get("length"))),
                       "album": _text(f.get("album") or o.get("album"))})
    albums = [t["album"] for t in tracks if t["album"]]
    tracks.sort(key=lambda t: (t["n"] is None, t["n"] or 0, t["file"].lower()))
    year = md.get("year") or str(md.get("date") or "")[:4]
    item = {"id": ident, "title": _text(md.get("title")) or ident, "artist": _text(md.get("creator")),
            "year": int(year) if str(year).isdigit() else None,
            "about": _text(md.get("description"))[:600], "tracks": tracks[:300], "count": len(tracks),
            "album": _text(md.get("album")) or (max(albums, key=albums.count) if albums else "")}
    net_cache[ident] = item
    return item


def net_info(ids):
    """Song count and what kind of record each search result is (album, single, live show…)."""
    def one(ident):
        try:
            it = net_item(ident)
        except (HTTPError, URLError, OSError, ValueError):
            return ident, None
        if not it:
            return ident, None
        n = it.get("count") or len(it["tracks"])
        kind = "Single" if n <= 3 else "Album" if n <= 30 else "Collection"
        album = it.get("album") or ""
        # only worth saying when the album isn't just the record's own title
        if album.lower() in it["title"].lower() or re.match(r"(unknown|untitled|my (documents|music)|new folder)", album, re.I):
            album = ""
        return ident, {"songs": n, "kind": kind, "album": album}
    with ThreadPoolExecutor(max_workers=12) as ex:
        return {k: v for k, v in ex.map(one, ids[:60]) if v}


# ---------------------------------------------------------------- internet radio (radio-browser.info)

RADIO_HOSTS = ["https://de1.api.radio-browser.info", "https://de2.api.radio-browser.info"]
RADIO_CODECS = {"MP3", "AAC", "AAC+", "OGG"}
radio_by_id = {}  # uuid -> station (stream url included, never sent from the browser)


def radio_get(path, params=None):
    err = None
    for host in RADIO_HOSTS:
        try:
            return ia_get(host + path, params, timeout=12)
        except (HTTPError, URLError, OSError, ValueError) as e:
            err = e
    raise err


def _station(s):
    st = {"id": s["stationuuid"], "name": _text(s.get("name")) or "Unnamed station",
          "tags": ", ".join(t for t in (s.get("tags") or "").split(",")[:4] if t),
          "country": s.get("countrycode") or "", "codec": s.get("codec") or "", "bitrate": s.get("bitrate") or 0,
          "favicon": s.get("favicon") or "", "clicks": s.get("clickcount") or 0, "url": s.get("url_resolved") or s.get("url")}
    radio_by_id[st["id"]] = st
    return st


def radio_search(q):
    base = {"hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": 60}
    words = " ".join(re.findall(r"[\w'&+.-]+", q))[:60]
    if words:
        found = radio_get("/json/stations/search", {**base, "name": words}) + radio_get("/json/stations/search", {**base, "tag": words.lower()})
    else:
        found = radio_get("/json/stations/search", {**base, "countrycode": "GB", "limit": 80})
    seen, out = set(), []
    for s in sorted(found, key=lambda s: -(s.get("clickcount") or 0)):
        if s["stationuuid"] in seen or s.get("hls") or (s.get("codec") or "").upper() not in RADIO_CODECS:
            continue
        seen.add(s["stationuuid"])
        out.append(_station(s))
    return [{k: v for k, v in s.items() if k != "url"} for s in out[:48]]


def radio_url(uuid):
    if uuid not in radio_by_id:
        found = radio_get("/json/stations/byuuid/" + uuid)
        if not found:
            return None
        _station(found[0])
    return radio_by_id[uuid]["url"]


# ---------------------------------------------------------------- podcasts (Apple's podcast directory + each show's RSS feed)

import xml.etree.ElementTree as ET

ITUNES = "https://itunes.apple.com"
POD_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SelectAndPlay/1.0 podcast"}
pod_cache = {}  # show id -> (time, show with episodes; enclosure urls stay on the server)
POD_TTL = 30 * 60


def _pod_show(r):
    return {"id": str(r["collectionId"]), "title": _text(r.get("collectionName")) or "Podcast",
            "artist": _text(r.get("artistName")), "art": r.get("artworkUrl600") or r.get("artworkUrl100") or "",
            "genre": r.get("primaryGenreName") or "", "count": r.get("trackCount") or 0,
            "latest": (r.get("releaseDate") or "")[:10], "feed": r.get("feedUrl") or ""}


def pod_search(q):
    words = " ".join(re.findall(r"[\w'&+.-]+", q))[:80]
    if not words:
        return []
    d = ia_get(ITUNES + "/search", {"media": "podcast", "entity": "podcast", "term": words, "limit": 40, "country": "GB"}, timeout=15)
    return [{k: v for k, v in _pod_show(r).items() if k != "feed"} for r in d.get("results", []) if r.get("feedUrl")]


def _dur(v):
    v = (v or "").strip()
    return round(_secs(v)) if re.fullmatch(r"[\d:.]+", v) else 0


def _date(v):
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(v).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        return ""


def pod_show(sid, fresh=False):
    hit = pod_cache.get(sid)
    if hit and not fresh and time.time() - hit[0] < POD_TTL:
        return hit[1]
    res = ia_get(ITUNES + "/lookup", {"id": sid, "entity": "podcast", "country": "GB"}, timeout=15).get("results") or []
    if res and res[0].get("feedUrl"):
        show = _pod_show(res[0])
    else:
        # a few shows are hidden from Apple's lookup: pod_feeds.json gives their feed by hand
        extra = load_json(os.path.join(DATA_DIR, "pod_feeds.json"), {}).get(sid)
        if not extra:
            return None
        show = {"id": sid, "count": 0, "latest": "", **extra}
    with urlopen(Request(show["feed"], headers=POD_UA), timeout=25) as r:
        root = ET.fromstring(r.read())
    ch = root.find("channel")
    if ch is None:
        return None
    it_ns = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    show["about"] = _text(ch.findtext("description") or ch.findtext(it_ns + "summary"))[:600]
    eps = []
    for item in ch.findall("item"):
        enc_el = item.find("enclosure")
        url = enc_el.get("url") if enc_el is not None else None
        if not url or not (enc_el.get("type", "audio").startswith("audio") or re.search(r"\.(mp3|m4a|aac|ogg)(\?|$)", url, re.I)):
            continue
        guid = (item.findtext("guid") or url).strip()
        eps.append({"key": hashlib.md5(guid.encode()).hexdigest()[:12], "title": _text(item.findtext("title")) or "Episode",
                    "date": _date(item.findtext("pubDate")), "dur": _dur(item.findtext(it_ns + "duration")),
                    "about": _text(item.findtext("description") or item.findtext(it_ns + "summary"))[:400], "url": url})
    eps.sort(key=lambda e: e["date"], reverse=True)
    show["episodes"] = eps[:300]
    show["total"] = len(eps)
    pod_cache[sid] = (time.time(), show)
    return show


def pod_public(show):
    out = {k: v for k, v in show.items() if k not in ("feed", "episodes")}
    out["episodes"] = [{k: v for k, v in e.items() if k != "url"} for e in show["episodes"]]
    return out


POD_STOP = set("""the a an and or of to in on for with at by from is are was were be been it its this that these those you your i me my we our he she his her they them their
what who how why when where which not no yes but if so as do does did has have had will would can could just about into over after before more most new episode
part ep vs v us uk podcast show live special bonus one two three all out up off than then there here also only very really get got gets
down different every some feel leave life take led woman man men women people years year time day days week back like make made know way first last next much many other another
been being could should would might must says said tell told talk talks talking story stories look looks inside behind through while still even ever never always
again best good great big little long old young today tonight world thing things lot lots want need help home questions question answer real true whole
case cases full latest series season chapter episodes listen listener listeners hear join host hosts guest guests welcome discuss discusses""".split())


def _words(text):
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", (text or "").lower()) if w not in POD_STOP}


suggest_cache = {}  # request key -> (time, result)


def pod_chart(genre_id, n=25):
    d = ia_get(f"{ITUNES}/gb/rss/toppodcasts/limit={n}/genre={genre_id}/json", timeout=15)
    out = []
    for e in (d.get("feed") or {}).get("entry") or []:
        try:
            out.append(e["id"]["attributes"]["im:id"])
        except (KeyError, TypeError):
            pass
    return out


def pod_suggest(weights, words, skip, heard):
    """Episodes from shows you don't follow, in the genres you listen to most, best keyword match first.
    weights = {show id: how much you listen}, words = titles/notes of episodes you heard,
    skip = show ids not to suggest (followed or dismissed), heard = episode ids already played."""
    key = json.dumps([sorted(weights.items()), sorted(words)[:200], sorted(skip)])
    hit = suggest_cache.get(key)
    if hit and time.time() - hit[0] < POD_TTL:
        return hit[1]
    ids = list(weights)[:150]
    looked = ia_get(ITUNES + "/lookup", {"id": ",".join(ids), "entity": "podcast", "country": "GB"}, timeout=15).get("results") or [] if ids else []
    g_w, g_name = {}, {}
    for r in looked:
        gid = next((g for g in (r.get("genreIds") or []) if g != "26"), None)
        if gid:
            g_w[gid] = g_w.get(gid, 0) + weights.get(str(r.get("collectionId")), 1)
            g_name[gid] = r.get("primaryGenreName") or ""
    for sid in ids:  # shows Apple's lookup hides (pod_feeds.json) still count
        extra = load_json(os.path.join(DATA_DIR, "pod_feeds.json"), {}).get(sid)
        if extra and not any(str(r.get("collectionId")) == sid for r in looked):
            gid = {"True Crime": "1488", "Comedy": "1303", "News": "1489", "History": "1487"}.get(extra.get("genre"))
            if gid:
                g_w[gid] = g_w.get(gid, 0) + weights.get(sid, 1)
                g_name[gid] = extra.get("genre")
    if not g_w:  # nothing to go on yet: the UK's overall chart
        g_w, g_name = {"26": 1}, {"26": "Popular"}
    top = sorted(g_w, key=g_w.get, reverse=True)[:4]
    total = sum(g_w[g] for g in top)
    picks = []  # (show id, genre id, chart rank)
    with ThreadPoolExecutor(max_workers=4) as ex:
        charts = list(ex.map(lambda g: (g, pod_chart(g)), top))
    for g, chart in charts:
        n = max(2, round(14 * g_w[g] / total))
        fresh = [sid for sid in chart if sid not in skip and sid not in weights][:n]
        picks += [(sid, g, i) for i, sid in enumerate(fresh)]
    def load(p):
        try:
            return p, pod_show(p[0])
        except (HTTPError, URLError, OSError, ValueError, ET.ParseError):
            return p, None
    with ThreadPoolExecutor(max_workers=10) as ex:
        loaded = list(ex.map(load, picks))
    today = time.strftime("%Y-%m-%d")
    eps, shows = [], []
    for (sid, g, rank), show in loaded:
        if not show:
            continue
        shows.append({k: v for k, v in show.items() if k not in ("feed", "episodes", "about")} | {"why": g_name.get(g, "")})
        for e in show["episodes"][:6]:
            if f"pod-{sid}~{e['key']}" in heard:
                continue
            match = _words(e["title"] + " " + e["about"]) & words
            try:
                age = (time.mktime(time.strptime(today, "%Y-%m-%d")) - time.mktime(time.strptime(e["date"], "%Y-%m-%d"))) / 86400
            except ValueError:
                age = 365
            score = len(match) * 3 + g_w[g] / total * 6 - rank * 0.15 - min(age, 120) / 15
            eps.append({"score": round(score, 2), "show": sid, "match": sorted(match)[:4], "why": g_name.get(g, ""),
                        **{k: v for k, v in e.items() if k != "url"}})
    eps.sort(key=lambda e: e["score"], reverse=True)
    out, per = [], {}
    for e in eps:  # at most two from any one show
        if per.get(e["show"], 0) < 2:
            per[e["show"]] = per.get(e["show"], 0) + 1
            out.append(e)
    res = {"genres": [g_name.get(g, "") for g in top], "shows": shows, "episodes": out[:24]}
    suggest_cache[key] = (time.time(), res)
    return res


def pod_url(sid, key):
    for fresh in (False, True):  # a new episode may not be in the cached copy yet
        show = pod_show(sid, fresh)
        for e in (show or {}).get("episodes", []):
            if e["key"] == key:
                return e["url"]
    return None


# ---------------------------------------------------------------- videos (YouTube search via yt-dlp, Internet Archive films)

try:
    import yt_dlp
except ImportError:
    yt_dlp = None
NEED_YTDLP = "Video search needs yt-dlp. Run:  python -m pip install --user yt-dlp"
VIDEO_N = 30
IA_VIDEO_FORMATS = ["h.264", "h.264 IA", "MPEG4", "512Kb MPEG4", "h.264 HD"]  # first = preferred
video_cache = {}   # (src, q) -> (time, result)
yt_fmt_cache = {}  # video id -> (time, formats to merge)


def yt_search(q):
    if not yt_dlp:
        raise RuntimeError(NEED_YTDLP)
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}) as y:
        r = y.extract_info(f"ytsearch{VIDEO_N}:{q}", download=False)
    items = []
    for e in r.get("entries") or []:
        if not e or not e.get("id") or e.get("ie_key") not in (None, "Youtube"):  # skip channels and playlists
            continue
        items.append({"id": e["id"], "src": "yt", "title": _text(e.get("title")) or "Untitled",
                      "by": _text(e.get("channel") or e.get("uploader")), "dur": round(e.get("duration") or 0),
                      "views": e.get("view_count") or 0, "live": e.get("live_status") == "is_live",
                      "thumb": f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg"})
    return {"items": items}


def ia_video_search(q):
    words = re.findall(r"[\w']+", q.lower())[:8]
    if not words:
        return {"items": []}
    w = " AND ".join(words)
    d = ia_get(IA + "/advancedsearch.php", {
        "q": f"(title:({w}) OR creator:({w}) OR subject:({w})) AND mediatype:(movies)",
        "fl[]": ["identifier", "title", "creator", "downloads"], "sort[]": "downloads desc",
        "rows": VIDEO_N, "page": 1, "output": "json"})["response"]
    return {"items": [{"id": x["identifier"], "src": "ia", "title": _text(x.get("title")) or x["identifier"],
                       "by": _text(x.get("creator")), "dur": 0, "views": x.get("downloads") or 0, "live": False,
                       "thumb": f"{IA}/services/img/{quote(x['identifier'])}"} for x in d["docs"]]}


def video_search(q, src):
    key = (src, q.lower())
    hit = video_cache.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    res = ia_video_search(q) if src == "ia" else yt_search(q)
    video_cache[key] = (time.time(), res)
    return res


def ia_video_file(ident):
    """The best playable video file in an Internet Archive item."""
    d = ia_get(f"{IA}/metadata/{quote(ident)}")
    files = [f for f in d.get("files", []) if f.get("private") != "true"]
    ranked = sorted((f for f in files if f.get("format") in IA_VIDEO_FORMATS), key=lambda f: IA_VIDEO_FORMATS.index(f["format"]))
    ranked += [f for f in files if f["name"].lower().endswith(".mp4") and f not in ranked]
    if not ranked:
        return None
    f = ranked[0]
    return {"file": f["name"], "dur": round(_secs(f.get("length")))}


def yt_formats(vid):
    """Video + audio streams for playing a clip through the jukebox when YouTube won't let it be embedded."""
    hit = yt_fmt_cache.get(vid)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    if not yt_dlp:
        raise RuntimeError(NEED_YTDLP)
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                           "format": "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b"}) as y:
        info = y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False)
    fmts = info.get("requested_formats") or [info]
    yt_fmt_cache[vid] = (time.time(), fmts)
    return fmts


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_bytes(self, data, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8", code, {"Cache-Control": "no-store"})

    def not_found(self):
        self.send_bytes(b"not found", "text/plain", 404)

    def send_file(self, path, ctype):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                code = 206
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=86400")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send_bytes(f.read(), "text/html; charset=utf-8", extra={"Cache-Control": "no-store"})
        if path == "/api/library":
            pub = lambda a: {k: v for k, v in a.items() if not k.startswith("_")}
            st = settings()
            return self.send_json({"albums": [pub(a) for a in library["albums"]], "copyTo": st.get("copyTo") or "",
                                   "tracks": library["tracks"], "moodDefs": library.get("moodDefs", {}),
                                   "name": st.get("name") or DEFAULT_NAME, "needsSetup": not st.get("done"),
                                   "folders": music_folders()})
        if path == "/api/setup/find":
            st = settings()
            return self.send_json({"found": find_music(), "folders": music_folders(sources=True), "name": st.get("name") or DEFAULT_NAME,
                                   "copyTo": st.get("copyTo") or "", "copyDefault": default_copy_folder()})
        if path == "/api/sync":
            return self.send_json(sync_state)
        if path == "/api/setup/browse":
            f = pick_folder()
            if not f:
                return self.send_json({"path": ""})
            n, sample, more = count_audio(f)
            return self.send_json({"path": f, "songs": n, "more": more})
        if path == "/api/state":
            with state_lock:
                return self.send_json(load_json(STATE_FILE, {}))
        m = re.match(r"^/audio/(\w+)$", path)
        if m:
            t = track_by_id.get(m.group(1))
            if not t:
                return self.not_found()
            if t["needsConvert"]:
                out = ensure_converted(t)
                return self.send_file(out, "audio/mpeg") if out else self.send_bytes(b"cannot convert", "text/plain", 415)
            full = os.path.join(MUSIC_ROOT, t["path"])
            return self.send_file(full, mimetypes.guess_type(full)[0] or "audio/mp4")
        m = re.match(r"^/art/(\w+)$", path)
        if m:
            a = album_by_id.get(m.group(1))
            if not a:
                return self.not_found()
            if a["_cover"]:
                return self.send_file(os.path.join(MUSIC_ROOT, a.get("_coverDir", a["path"]), a["_cover"]), "image/jpeg")
            if a["_artSrc"]:
                data, mime = embedded_art(os.path.join(MUSIC_ROOT, a["_artSrc"]))
                if data:
                    return self.send_bytes(data, mime, extra={"Cache-Control": "max-age=86400"})
            return self.not_found()
        m = re.match(r"^/api/lyrics/(\w+)$", path)
        if m:
            t = track_by_id.get(m.group(1))
            if not t:
                return self.not_found()
            return self.send_json({"lyrics": read_tags(os.path.join(MUSIC_ROOT, t["path"])).get("lyrics")})
        if path == "/api/net/search":
            qs = parse_qs(urlparse(self.path).query)
            try:
                return self.send_json(net_search(qs.get("q", [""])[0], qs.get("where", ["music"])[0], int(qs.get("page", ["1"])[0])))
            except (HTTPError, URLError, OSError, ValueError, KeyError) as e:
                return self.send_json({"error": f"Internet Archive didn't answer ({e})"}, 502)
        if path == "/api/net/info":
            ids = [i for i in parse_qs(urlparse(self.path).query).get("ids", [""])[0].split(",") if re.fullmatch(r"[\w.\-]+", i)]
            return self.send_json(net_info(ids))
        m = re.match(r"^/api/net/item/([\w.\-]+)$", path)
        if m:
            try:
                item = net_item(m.group(1))
            except (HTTPError, URLError, OSError, ValueError) as e:
                return self.send_json({"error": f"Internet Archive didn't answer ({e})"}, 502)
            return self.send_json(item) if item else self.not_found()
        m = re.match(r"^/net/(?:audio|video)/([\w.\-]+)/(.+)$", path)
        if m:
            return self.relay(f"{IA}/download/{m.group(1)}/{quote(m.group(2))}")
        if path == "/api/radio/search":
            try:
                return self.send_json(radio_search(parse_qs(urlparse(self.path).query).get("q", [""])[0]))
            except (HTTPError, URLError, OSError, ValueError) as e:
                return self.send_json({"error": f"The radio directory didn't answer ({e})"}, 502)
        m = re.match(r"^/radio/stream/([\w\-]+)$", path)
        if m:
            try:
                url = radio_url(m.group(1))
            except (HTTPError, URLError, OSError, ValueError):
                url = None
            return self.relay(url, live=True) if url else self.send_bytes(b"station not found", "text/plain", 404)
        if path == "/api/update":
            return self.send_json(check_update())
        if path == "/api/pod/import":
            # shows dropped in pods_import.json get followed next time the page opens (file is used once)
            f = os.path.join(DATA_DIR, "pods_import.json")
            shows = load_json(f, [])
            if shows:
                os.replace(f, f + ".done")
            return self.send_json(shows)
        if path == "/api/pod/search":
            try:
                return self.send_json(pod_search(parse_qs(urlparse(self.path).query).get("q", [""])[0]))
            except (HTTPError, URLError, OSError, ValueError) as e:
                return self.send_json({"error": f"The podcast directory didn't answer ({e})"}, 502)
        m = re.match(r"^/api/pod/show/(\d+)$", path)
        if m:
            fresh = "fresh" in parse_qs(urlparse(self.path).query)
            try:
                show = pod_show(m.group(1), fresh)
            except (HTTPError, URLError, OSError, ValueError, ET.ParseError) as e:
                return self.send_json({"error": f"Couldn't get that show's episodes ({e})"}, 502)
            return self.send_json(pod_public(show)) if show else self.send_json({"error": "Show not found"}, 404)
        if path == "/api/pod/suggest":
            qs = parse_qs(urlparse(self.path).query)
            weights = {}
            for part in qs.get("w", [""])[0].split(","):
                sid, _, w = part.partition(":")
                if sid.isdigit():
                    weights[sid] = float(w) if re.fullmatch(r"[\d.]+", w) else 1.0
            words = set(qs.get("words", [""])[0].lower().split()[:300])
            skip = {x for x in qs.get("skip", [""])[0].split(",") if x.isdigit()}
            heard = set(qs.get("heard", [""])[0].split(","))
            try:
                return self.send_json(pod_suggest(weights, words, skip, heard))
            except (HTTPError, URLError, OSError, ValueError) as e:
                return self.send_json({"error": f"Couldn't get suggestions ({e})"}, 502)
        m = re.match(r"^/pod/audio/(\d+)/([0-9a-f]{12})$", path)
        if m:
            try:
                url = pod_url(m.group(1), m.group(2))
            except (HTTPError, URLError, OSError, ValueError, ET.ParseError):
                url = None
            return self.relay(url, pod=True) if url else self.send_bytes(b"episode not found", "text/plain", 404)
        if path == "/api/video/search":
            qs = parse_qs(urlparse(self.path).query)
            q, src = qs.get("q", [""])[0].strip()[:120], qs.get("src", ["yt"])[0]
            if not q:
                return self.send_json({"items": []})
            try:
                return self.send_json(video_search(q, src))
            except Exception as e:
                who = "The Internet Archive" if src == "ia" else "YouTube"
                msg = str(e) if isinstance(e, RuntimeError) else f"{who} didn't answer ({e})"
                return self.send_json({"error": msg[:300]}, 502)
        m = re.match(r"^/api/video/ia/([\w.\-]+)$", path)
        if m:
            try:
                f = ia_video_file(m.group(1))
            except (HTTPError, URLError, OSError, ValueError) as e:
                return self.send_json({"error": f"The Internet Archive didn't answer ({e})"}, 502)
            return self.send_json(f) if f else self.send_json({"error": "No playable video in that one"}, 404)
        m = re.match(r"^/video/yt/([\w\-]{11})$", path)
        if m:
            t = parse_qs(urlparse(self.path).query).get("t", ["0"])[0]
            return self.yt_stream(m.group(1), int(t) if t.isdigit() else 0)
        self.not_found()

    def yt_stream(self, vid, start=0):
        """Play a YouTube clip through the jukebox: ffmpeg joins its video and sound into one stream."""
        if self.command == "HEAD":
            return self.send_bytes(b"", "video/mp4")
        if not FFMPEG:
            return self.send_bytes(b"ffmpeg missing", "text/plain", 501)
        try:
            fmts = yt_formats(vid)
        except Exception as e:
            return self.send_bytes(str(e)[:300].encode(), "text/plain", 502)
        args = [FFMPEG, "-v", "error", "-nostdin"]
        for f in fmts:
            if start:
                args += ["-ss", str(start)]
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items()), "-i", f["url"]]
        args += ["-map", "0:v:0", "-map", "1:a:0" if len(fmts) > 1 else "0:a:0?", "-c", "copy",
                 "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            while True:
                chunk = p.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            pass
        finally:
            p.kill()

    def relay(self, url, live=False, pod=False):
        """Stream an internet file or live station through, passing Range on so seeking works."""
        # a browser-like agent makes old SHOUTcast servers answer in HTTP rather than "ICY 200"
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) SelectAndPlay/1.0"} if live else dict(POD_UA if pod else UA)
        if self.headers.get("Range") and not live:
            hdrs["Range"] = self.headers["Range"]
        try:
            r = urlopen(Request(url, headers=hdrs), timeout=30)
        except HTTPError as e:
            return self.send_bytes(b"", "text/plain", e.code)
        except (URLError, OSError):
            return self.send_bytes(b"offline", "text/plain", 502)
        with r:
            self.send_response(r.status)
            for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if r.headers.get(k):
                    v = r.headers[k]
                    self.send_header(k, "audio/aac" if k == "Content-Type" and "aacp" in v else v)
            self.send_header("Cache-Control", "no-store" if live else "max-age=3600")
            if not r.headers.get("Content-Length"):
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
                pass

    def do_POST(self):
        path = urlparse(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path == "/api/state":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self.send_json({"error": "bad json"}, 400)
            with state_lock:
                save_json(STATE_FILE, data)
            if "albumMoods" in data:
                apply_curation(library["albums"])
            return self.send_json({"ok": True})
        if path == "/api/update/run":
            return self.send_json({"ok": run_update()})
        if path == "/api/setup":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self.send_json({"error": "bad json"}, 400)
            folders = [os.path.normpath(f) for f in data.get("folders", []) if isinstance(f, str) and os.path.isdir(f)]
            name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()[:40] or DEFAULT_NAME
            copy_to = data.get("copyTo") or ""
            copy_to = os.path.normpath(copy_to) if isinstance(copy_to, str) and copy_to.strip() else None
            save_json(SETTINGS_FILE, {**settings(), "folders": folders, "name": name, "done": True, "copyTo": copy_to})
            scan()
            if copy_to:
                threading.Thread(target=sync_copy, daemon=True).start()
            threading.Thread(target=prewarm, daemon=True).start()
            return self.send_json({"ok": True, "albums": len(library["albums"]), "tracks": len(library["tracks"])})
        if path == "/api/rescan":
            scan()
            threading.Thread(target=prewarm, daemon=True).start()
            return self.send_json({"ok": True})
        self.not_found()


def main():
    lan = "--lan" in sys.argv
    scan()
    if not FFMPEG:
        print("WARNING: ffmpeg not found - WMA tracks will not play. Run: python -m pip install --user imageio-ffmpeg")
    else:
        threading.Thread(target=prewarm, daemon=True).start()
    host = "0.0.0.0" if lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Jukebox running at {url}  (Ctrl+C to stop)")
    if lan:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            print(f"On your phone/tablet (same Wi-Fi): http://{s.getsockname()[0]}:{PORT}/")
            s.close()
        except OSError:
            pass
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
