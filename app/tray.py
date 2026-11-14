"""The jukebox as a tray app.

It sits in the notification area (by the clock). Click the icon to open the window;
right-click for Play/Pause, Next, Previous and Quit. Closing the window hides it back
to the tray and the music keeps playing. Started with --hidden (as it is when Windows
starts) it goes straight to the tray without opening the window.
Opening it again from the desktop or Start menu while it runs just shows the window.
"""
import ctypes
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pystray
import webview
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
import server  # noqa: E402

ICON_PATH = os.path.join(HERE, "wurlitzer.ico")
ICON_PNG = os.path.join(HERE, "wurlitzer.png")
DATA_DIR = os.path.join(server.DATA_DIR, "webview")
APP_ID = "SelectAndPlay"
SHOW_EVENT = "SelectAndPlayShow"

kernel32 = ctypes.windll.kernel32
window = None
tray = None
quitting = False


def app_name():
    return server.settings().get("name") or server.DEFAULT_NAME


# If another app has grabbed 8765 before us, use one of these instead.
FALLBACK_PORTS = range(48765, 48775)


class OwnServer(ThreadingHTTPServer):
    """Binds the port exclusively, so no other program can share or steal it while
    the jukebox runs (Windows otherwise lets a second app bind the same port)."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def start_server():
    """Start the jukebox server on its own port and return that port. The window
    always loads the server started here, so it can never show another app's page."""
    srv = None
    for port in (server.PORT, *FALLBACK_PORTS):
        try:
            srv = OwnServer(("127.0.0.1", port), server.Handler)
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit("Jukebox: no free port")
    server.scan()
    threading.Thread(target=server.sync_loop, daemon=True).start()
    if server.FFMPEG:
        threading.Thread(target=server.prewarm, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port


def show(*_):
    window.show()
    window.restore()
    window.on_top = True  # bring it in front of whatever else is open
    window.on_top = False


def js(code):
    try:
        window.evaluate_js(code)
    except Exception:
        pass


def quit_app(*_):
    global quitting
    quitting = True
    try:
        tray.stop()
    except Exception:
        pass
    window.destroy()


def on_closing():
    """The X hides the window to the tray instead of quitting (Quit is on the tray menu)."""
    if quitting:
        return True
    threading.Timer(0.05, window.hide).start()  # hide just after the close is cancelled
    return False


def make_tray():
    menu = pystray.Menu(
        pystray.MenuItem("Open", show, default=True),  # default = what a left-click does
        pystray.MenuItem("Play / Pause", lambda *_: js("toggle()")),
        pystray.MenuItem("Next", lambda *_: js("next(false)")),
        pystray.MenuItem("Previous", lambda *_: js("prev()")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    return pystray.Icon(APP_ID, Image.open(ICON_PNG), app_name(), menu)


def keep_icon_visible():
    """Windows 11 tucks new tray icons behind the ^ arrow. Mark ours as shown by the
    clock (applies from the next sign-in). Only touches entries with our name on them."""
    try:
        import winreg
        root = r"Control Panel\NotifyIconSettings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                with winreg.OpenKey(k, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as s:
                    try:
                        tip = winreg.QueryValueEx(s, "InitialTooltip")[0] or ""
                        exe = winreg.QueryValueEx(s, "ExecutablePath")[0] or ""
                    except OSError:
                        continue
                    if exe.lower().endswith("pythonw.exe") and tip.startswith(app_name()):
                        winreg.SetValueEx(s, "IsPromoted", 0, winreg.REG_DWORD, 1)
    except OSError:
        pass


def wait_for_show(event):
    """Another launch (desktop or Start menu icon) signals this event: show our window."""
    while not quitting:
        if kernel32.WaitForSingleObject(event, 1000) == 0:  # WAIT_OBJECT_0
            try:
                show()
            except Exception:
                pass


def watch_title():
    """Keep the window title and the tray tooltip showing what is playing."""
    last = None
    while not quitting:
        time.sleep(3)
        try:
            now = window.evaluate_js(
                "(() => { const t = byT[Q[qi]]; if (!t) return ''; "
                "return (audio.paused ? 'Paused: ' : '') + t.title + ' — ' + t.artist; })()")
        except Exception:
            continue
        name = app_name()
        text = name + " — " + now if now else name
        if text != last:
            window.set_title(text)
            try:
                tray.title = text[:120]  # Windows caps tray tooltips at 127 characters
            except Exception:
                pass
            last = text


def main():
    global window, tray
    hidden = "--hidden" in sys.argv
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    # auto-reset event, created by the first copy; later copies just set it and leave
    event = kernel32.CreateEventW(None, False, False, SHOW_EVENT)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        if not hidden:
            kernel32.SetEvent(event)
        return

    # a newer version on GitHub? put it in place before anything starts, then run the new copy
    try:
        import autoupdate
        if autoupdate.run(HERE, server.DATA_DIR):
            import subprocess
            kernel32.CloseHandle(event)  # so the new copy doesn't think one is already running
            subprocess.Popen([sys.executable, os.path.join(HERE, "tray.py")] + sys.argv[1:], cwd=HERE, close_fds=True)
            return
    except Exception:
        pass  # never let an update problem stop the jukebox opening

    port = start_server()
    url = f"http://127.0.0.1:{port}/"
    window = webview.create_window(app_name(), url, width=1320, height=860, min_size=(900, 600),
                                   background_color="#1b1210", hidden=hidden,
                                   x=int(os.environ["SELECTANDPLAY_X"]) if os.environ.get("SELECTANDPLAY_X") else None,  # tests only: open off-screen
                                   y=100 if os.environ.get("SELECTANDPLAY_X") else None)
    window.events.closing += on_closing
    # pywebview only honours "return False" (cancel the close) when the handler runs
    # straight away, so make this event run its handlers in step
    window.events.closing._should_lock = True
    tray = make_tray()
    tray.run_detached()
    threading.Thread(target=wait_for_show, args=(event,), daemon=True).start()
    threading.Thread(target=watch_title, daemon=True).start()
    threading.Timer(10, keep_icon_visible).start()

    os.makedirs(DATA_DIR, exist_ok=True)
    webview.start(private_mode=False, storage_path=DATA_DIR, icon=ICON_PATH)
    # the window has gone (Quit, or Windows shutting down): take the tray icon with it
    global quitting
    quitting = True
    try:
        tray.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
