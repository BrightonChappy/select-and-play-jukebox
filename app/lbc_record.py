"""Record an LBC show off the free live stream so it turns up in the jukebox as a podcast.

Run by Task Scheduler a minute before the show:  pythonw lbc_record.py dunham   (or barnett)
Each show keeps its newest KEEP recordings in <data folder>/lbc/<show>/; the jukebox reads that folder
(pod_feeds.json "dir") and lists every file as an episode.
"""
import datetime
import glob
import os
import subprocess
import sys

# recordings live in the data folder with everything else personal, so updates never touch them
HERE = os.environ.get("SELECTANDPLAY_DATA") or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SelectAndPlay")
STREAM = "https://media-ssl.musicradio.com/LBCUK"
SHOWS = {
    "dunham": "Dean Dunham KC",
    "barnett": "Legal Hour with Daniel Barnett",
}
SECONDS = 63 * 60   # started at 20:59, runs to about 22:02 so the hour isn't clipped
KEEP = 10
LOG = os.path.join(HERE, "lbc", "record.log")


def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M} {msg}\n")


def main():
    show = (sys.argv[1:] or [""])[0]
    if show not in SHOWS:
        sys.exit("usage: lbc_record.py " + "|".join(SHOWS))
    folder = os.path.join(HERE, "lbc", show)
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.datetime.now()
    out = os.path.join(folder, f"{stamp:%Y-%m-%d}.m4a")
    part = out + ".part.m4a"   # the jukebox only lists finished .m4a files without ".part"
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else SECONDS
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
           "-i", STREAM, "-t", str(secs), "-c", "copy", "-bsf:a", "aac_adtstoasc",
           "-movflags", "+faststart", "-metadata", f"title={SHOWS[show]} - {stamp:%A %d %B %Y}", part]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    size = os.path.getsize(part) if os.path.exists(part) else 0
    if res.returncode != 0 or size < secs * 4000:   # the stream is 48 kb/s = 6 KB a second
        log(f"{show}: SHORT/FAILED rc={res.returncode} {size // 1000} KB {res.stderr.strip()[-300:]}")
        if size >= 1_000_000:
            os.replace(part, out)   # keep a partial show rather than lose it
        elif size:
            os.remove(part)
        return
    os.replace(part, out)
    log(f"{show}: saved {os.path.basename(out)} ({os.path.getsize(out) // 1_000_000} MB)")
    for old in sorted(glob.glob(os.path.join(folder, "*.m4a")))[:-KEEP]:
        if ".part" not in old:
            os.remove(old)


if __name__ == "__main__":
    main()
