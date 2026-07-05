"""
Claude Code Usage widget — frameless, always-on-top HUD.
UI in index.html; data/math in engine.py. Features:
  - collapse/expand (slim pill <-> full card), persisted in state.json
  - global Ctrl+Alt+U to show/hide the HUD without touching the taskbar
  - auto-refresh: a config.txt save (a fresh /usage paste) is picked up within
    ~15s, plus a 10-minute heartbeat so the gauges never sit stale
Launch silently with run.vbs, or:  pyw app.py
"""
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
import webview
import engine

# DPI-aware so 1 logical px == 1 CSS px on scaled displays (else content clips).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ICON = HERE / "Branding" / "ClaudeCode_Icon.ico"
HTML = HERE / "index.html"
STATE = HERE / "state.json"
BG = "#191919"                                          # Slate Dark page
EXPANDED = (520, 450)   # generous initial height; JS fit_height trims to the card
COLLAPSED = (340, 62)


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(d):
    try:
        STATE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


class Api:
    def refresh(self):
        try:
            return engine.compute_gauges()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def get_state(self):
        return {"collapsed": bool(read_state().get("collapsed", False))}

    def set_collapsed(self, collapsed):
        collapsed = bool(collapsed)
        write_state({"collapsed": collapsed})
        try:
            webview.windows[0].resize(*(COLLAPSED if collapsed else EXPANDED))
        except Exception:
            pass
        return {"collapsed": collapsed}

    def fit_height(self, h):
        """Size the (expanded) window to the card's measured content height."""
        try:
            h = max(120, min(int(h), 1000))
            webview.windows[0].resize(EXPANDED[0], h)
        except Exception:
            pass
        return {"h": h}

    def quit(self):
        for w in list(webview.windows):
            w.destroy()


def set_window_icon(window):
    """Runtime octopus icon via Win32 WM_SETICON (icon= is unreliable on Windows)."""
    try:
        if not ICON.exists():
            return
        hwnd = window.native.Handle.ToInt32()
        for flag, cx in ((0, 16), (1, 32)):            # ICON_SMALL / ICON_BIG
            hicon = ctypes.windll.user32.LoadImageW(
                None, str(ICON), 1, cx, cx, 0x00000010)  # IMAGE_ICON, LR_LOADFROMFILE
            ctypes.windll.user32.SendMessageW(hwnd, 0x0080, flag, hicon)
    except Exception:
        pass


def start_hotkey(window):
    """Global Ctrl+Alt+U toggles HUD visibility (recall without the taskbar)."""
    try:
        hwnd = window.native.Handle.ToInt32()
    except Exception:
        return

    def loop():
        u = ctypes.windll.user32
        MOD = 0x0002 | 0x0001 | 0x4000                 # CONTROL | ALT | NOREPEAT
        if not u.RegisterHotKey(None, 1, MOD, 0x55):    # 0x55 = 'U'
            return                                      # already taken -> silently skip
        shown = [True]
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:                   # WM_HOTKEY
                shown[0] = not shown[0]
                u.ShowWindow(hwnd, 8 if shown[0] else 0)  # SW_SHOWNA / SW_HIDE
    threading.Thread(target=loop, daemon=True).start()


def start_autorefresh(window):
    """Repaint without user touches: whenever config.txt's save time changes
    (a paste just landed — its mtime is the capture stamp, so ingest is exact
    no matter when this fires), and every 10 min as a staleness heartbeat."""
    def loop():
        last, beat = None, 0
        while True:
            time.sleep(15)
            beat += 15
            try:
                mt = engine.CONFIG_PATH.stat().st_mtime
            except OSError:
                mt = None
            if (last is not None and mt != last) or beat >= 600:
                beat = 0
                try:
                    window.evaluate_js("doRefresh()")
                except Exception:
                    pass
            last = mt
    threading.Thread(target=loop, daemon=True).start()


def already_running():
    """One widget per machine — a second launch exits quietly."""
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "ClaudeUsage.Widget.Mutex")
        return ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def on_loaded(window):
    set_window_icon(window)
    start_hotkey(window)
    start_autorefresh(window)


def main():
    if already_running():
        sys.exit(0)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ClaudeUsage.Widget")
    except Exception:
        pass
    collapsed = bool(read_state().get("collapsed", False))
    w, h = COLLAPSED if collapsed else EXPANDED
    win = webview.create_window(
        "Claude Code Usage",
        url=HTML.as_uri() + ("?c=1" if collapsed else ""),
        js_api=Api(),
        width=w, height=h,
        frameless=True, easy_drag=False,               # targeted drag region in HTML
        on_top=True, resizable=False,
        background_color=BG,
    )
    win.events.loaded += lambda: on_loaded(win)
    webview.start(gui="edgechromium")


if __name__ == "__main__":
    main()
