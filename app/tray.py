"""Jukebox as a taskbar app: opens in its own window from the pinned taskbar icon.

Minimise the window and music keeps playing; click the taskbar icon to bring it back.
Clicking the pinned icon again while it's running just brings the window to the front.
Close the window to quit.
"""
import ctypes
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import webview

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
import server  # noqa: E402

ICON_PATH = os.path.join(HERE, "wurlitzer.ico")
DATA_DIR = os.path.join(server.DATA_DIR, "webview")
# must match System.AppUserModel.ID on the Jukebox shortcuts, so the running window
# groups under the pinned taskbar icon instead of appearing as a second one
APP_ID = "SelectAndPlay"
SHOW_EVENT = "SelectAndPlayShow"

kernel32 = ctypes.windll.kernel32
window = None
quitting = False


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


def wait_for_show(event):
    """Another launch (taskbar click) signals this event: bring our window forward."""
    while not quitting:
        if kernel32.WaitForSingleObject(event, 1000) == 0:  # WAIT_OBJECT_0
            try:
                show()
            except Exception:
                pass


def watch_title():
    """Keep the window title (and taskbar hover) showing what is playing."""
    last = None
    while not quitting:
        time.sleep(3)
        try:
            now = window.evaluate_js(
                "(() => { const t = byT[Q[qi]]; if (!t) return ''; "
                "return (audio.paused ? 'Paused: ' : '') + t.title + ' — ' + t.artist; })()")
        except Exception:
            continue
        name = server.settings().get("name") or server.DEFAULT_NAME
        text = name + " — " + now if now else name
        if text != last:
            window.set_title(text)
            last = text


def on_closed():
    global quitting
    quitting = True


def main():
    global window
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    # auto-reset event, created by the first copy; later copies just set it and leave
    event = kernel32.CreateEventW(None, False, False, SHOW_EVENT)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.SetEvent(event)
        return

    port = start_server()
    url = f"http://127.0.0.1:{port}/"
    window = webview.create_window(server.settings().get("name") or server.DEFAULT_NAME, url, width=1320, height=860, min_size=(900, 600),
                                   background_color="#1b1210")
    window.events.closed += on_closed
    threading.Thread(target=wait_for_show, args=(event,), daemon=True).start()
    threading.Thread(target=watch_title, daemon=True).start()

    os.makedirs(DATA_DIR, exist_ok=True)
    webview.start(private_mode=False, storage_path=DATA_DIR, icon=ICON_PATH)


if __name__ == "__main__":
    main()
