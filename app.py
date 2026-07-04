"""
PhoneCam — standalone receiver app (GUI).

A preview-first single-window application:
  - the phone's live image fills the whole window (embedded browser / hardware decode),
  - translucent overlays float on top: status dots, Settings + Fullscreen, and a Start/Stop button,
  - the QR code, setup steps, virtual-camera backend and tools live in a Settings dialog (gear / Ctrl+,),
  - runs the built-in server (HTTPS + WS signaling relay + internal aiortc→virtual-camera bridge).

Start:  pythonw app.py   (or run_app.ps1)
"""
import io
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer, QSettings, QUrl, QEvent
from PySide6.QtGui import QPixmap, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox,
    QHBoxLayout, QVBoxLayout, QGridLayout, QFrame, QPlainTextEdit, QSizePolicy,
    QDialog,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(HERE, "server")
sys.path.insert(0, SERVER_DIR)
import server as pcserver          # serve, build_ssl, make_args, lan_ip, Hooks
import make_cert                   # certificate (re)generation
import obs_control                 # OBS browser-mode auto-config (obs-websocket)

import qrcode

# ---- colors / style --------------------------------------------------------
BG, PANEL, LINE, TXT, MUT = "#0d1117", "#161b22", "#242c38", "#e6edf3", "#8b97a6"
ACC, OK, WARN, BAD = "#2ea043", "#3fb950", "#d29922", "#f85149"
INSET = "#0b0e13"
ELEV = "#1c232d"   # slightly lifted surface (hover / inner rows)

STYLE = f"""
QMainWindow, QWidget {{ background:{BG}; color:{TXT};
  font-family:'Segoe UI',sans-serif; font-size:13px; }}
QFrame#panel {{ background:{PANEL}; border:1px solid {LINE}; border-radius:16px; }}
QFrame#qrcard {{ background:{INSET}; border:1px solid {LINE}; border-radius:14px; }}
QFrame#steprow, QFrame#devcard, QFrame#statcell {{ background:{INSET};
  border:1px solid {LINE}; border-radius:12px; }}
QFrame#steprow:hover {{ border-color:{ACC}; }}
QLabel#preview {{ background:#000; border:1px solid {LINE}; border-radius:14px; color:{MUT};
  font-size:15px; }}
QLabel#rec {{ background:rgba(0,0,0,.55); border-radius:8px; padding:3px 9px;
  font-size:12px; font-weight:700; color:#fff; }}
QPushButton {{ background:{PANEL}; border:1px solid {LINE}; border-radius:10px;
  padding:9px 12px; color:{TXT}; font-weight:600; text-align:left; }}
QPushButton:hover {{ border-color:{ACC}; background:{ELEV}; }}
QPushButton:pressed {{ background:{INSET}; }}
QPushButton#tool {{ text-align:center; padding:9px 6px; font-size:12px; }}
QPushButton#primary {{ background:{ACC}; border:1px solid {OK}; color:#fff;
  padding:12px; font-size:14px; border-radius:12px; text-align:center; }}
QPushButton#primary:hover {{ background:{OK}; border-color:{OK}; }}
QPushButton#primary[running="true"] {{ background:#21262d; border-color:{BAD}; color:{BAD}; }}
QPushButton#primary[running="true"]:hover {{ background:#2a1618; }}
QPushButton#copy {{ background:{PANEL}; border:1px solid {LINE}; border-radius:8px;
  padding:5px 10px; color:{MUT}; font-size:11px; font-weight:600; text-align:center; }}
QPushButton#copy:hover {{ border-color:{ACC}; color:{TXT}; background:{ELEV}; }}
QPushButton#copy[done="true"] {{ color:{OK}; border-color:{OK}; }}
QComboBox {{ background:{PANEL}; border:1px solid {LINE}; border-radius:10px; padding:8px 10px; }}
QComboBox:hover {{ border-color:{ACC}; }}
QComboBox::drop-down {{ border:none; width:22px; }}
QComboBox QAbstractItemView {{ background:{PANEL}; border:1px solid {LINE};
  border-radius:8px; padding:4px; selection-background-color:{ACC}; outline:none; }}
QPlainTextEdit {{ background:{INSET}; border:1px solid {LINE}; border-radius:10px;
  color:{MUT}; font-family:Consolas,monospace; font-size:11px; padding:6px; }}
QLabel#h1 {{ font-size:19px; font-weight:800; letter-spacing:.2px; }}
QLabel#sub {{ color:{MUT}; font-size:11px; }}
QLabel#chip {{ background:{PANEL}; border:1px solid {LINE}; border-radius:999px;
  padding:5px 13px; font-size:12px; font-weight:600; color:{MUT}; }}
QLabel#chip[state="on"]   {{ background:rgba(63,185,80,.12);  border-color:rgba(63,185,80,.40); color:{TXT}; }}
QLabel#chip[state="wait"] {{ background:rgba(210,153,34,.12); border-color:rgba(210,153,34,.40); color:{TXT}; }}
QLabel#chip[state="bad"]  {{ background:rgba(248,81,73,.12);  border-color:rgba(248,81,73,.40); color:{TXT}; }}
QLabel#section {{ color:{MUT}; font-size:10.5px; font-weight:700; letter-spacing:1.2px; }}
QLabel#badge {{ background:{ACC}; color:#fff; font-size:12px; font-weight:800;
  border-radius:12px; min-width:24px; max-width:24px; min-height:24px; max-height:24px; }}
QLabel#badge[done="wait"] {{ background:{LINE}; color:{MUT}; }}
QLabel#steptitle {{ font-size:12.5px; font-weight:700; color:{TXT}; }}
QLabel#stepurl {{ color:{MUT}; font-family:Consolas,monospace; font-size:10.5px; }}
QLabel#qrcap {{ color:{MUT}; font-size:11px; }}
QFrame#sep {{ background:{LINE}; max-height:1px; min-height:1px; border:none; }}

/* ---- translucent overlays floating on the preview (preview-first layout) ---- */
QFrame#overlay {{ background:rgba(13,17,23,0.62); border:1px solid rgba(255,255,255,0.09);
  border-radius:13px; }}
QFrame#overlay QLabel#chip {{ background:transparent; border:none; padding:1px 3px; font-size:12px; }}
QFrame#overlaybar {{ background:transparent; border:none; }}
QPushButton#ovbtn {{ background:rgba(13,17,23,0.62); border:1px solid rgba(255,255,255,0.10);
  border-radius:12px; padding:9px 13px; color:{TXT}; font-weight:700; font-size:13px; text-align:center; }}
QPushButton#ovbtn:hover {{ background:rgba(30,37,46,0.82); border-color:rgba(63,185,80,0.55); }}
QPushButton#ovbtn:pressed {{ background:rgba(11,14,19,0.92); }}
QPushButton#ovtoggle {{ background:rgba(46,160,67,0.80); border:1px solid rgba(63,185,80,0.95);
  border-radius:16px; padding:12px 32px; color:#fff; font-size:15px; font-weight:800; text-align:center; }}
QPushButton#ovtoggle:hover {{ background:rgba(63,185,80,0.92); }}
QPushButton#ovtoggle:pressed {{ background:rgba(33,120,50,0.92); }}
QPushButton#ovtoggle[running="true"] {{ background:rgba(248,81,73,0.26);
  border-color:rgba(248,81,73,0.90); color:#ffd9d6; }}
QPushButton#ovtoggle[running="true"]:hover {{ background:rgba(248,81,73,0.42); color:#fff; }}
"""


# ---- thread-safe bridge for the server-thread callbacks --------------------
class Bridge(QObject):
    sig_ready = Signal(dict)
    sig_state = Signal(str, str)
    sig_frame = Signal(object)       # ndarray or None
    sig_vcam = Signal(bool, str)
    sig_log = Signal(str)
    sig_stopped = Signal(str)
    sig_stats = Signal(dict)         # res / fps / bitrate / loss
    sig_relay = Signal(str, bool)    # OBS path: sender/viewer connection
    sig_obs = Signal(bool, str)      # OBS auto-config result
    sig_meta = Signal(dict)          # phone meta (e.g. battery)
    sig_cert_done = Signal()         # cert generated off-thread -> launch worker (GUI thread)
    sig_cert_regen = Signal(bool)    # cert regenerated off-thread (arg: was_running) -> log + restart


class QtLogHandler(logging.Handler):
    def __init__(self, emit_fn):
        super().__init__()
        self.emit_fn = emit_fn

    def emit(self, record):
        try:
            self.emit_fn(self.format(record))
        except Exception:
            pass


class ServerWorker(threading.Thread):
    """Runs the server in its own asyncio loop; can be halted with stop()."""

    def __init__(self, args, hooks, bridge):
        super().__init__(daemon=True)
        self.args, self.hooks, self.bridge = args, hooks, bridge
        self.loop = None
        self.stop_event = None
        self._stop_requested = False   # set by stop() even before the loop/event exist (lost-wakeup guard)

    def run(self):
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            self.bridge.sig_stopped.emit(f"Server error: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self):
        import asyncio
        self.stop_event = asyncio.Event()
        if self._stop_requested:            # a stop() that landed before the event existed -> honour it now
            self.stop_event.set()
        ip = pcserver.lan_ip()
        ssl_ctx = pcserver.build_ssl(self.args.cert, self.args.key)
        await pcserver.serve(self.args, ssl_ctx, ip, self.hooks, self.stop_event)
        self.bridge.sig_stopped.emit("")

    def stop(self):
        self._stop_requested = True         # unconditional: covers the gap before loop/stop_event are set
        if self.loop and self.stop_event:
            try:
                self.loop.call_soon_threadsafe(self.stop_event.set)
            except Exception:
                pass


# ---- status chip (dot + label) ---------------------------------------------
class Dot(QLabel):
    def __init__(self, text):
        super().__init__()
        self.base = text
        self.setObjectName("chip")
        self.set("off")

    def set(self, kind, label=None):
        color = {"off": MUT, "on": OK, "wait": WARN, "bad": BAD}.get(kind, MUT)
        self.setText(f"<span style='color:{color}'>●</span>  {label or self.base}")
        self.setProperty("state", kind)
        self.style().unpolish(self)
        self.style().polish(self)


class _SettingsDialog(QDialog):
    """Modeless setup dialog. Escape hides it AND leaves fullscreen if the main window is in it,
    so a user in fullscreen who opened Settings isn't stuck when the dialog swallows the first Escape."""
    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
            if self._owner.isFullScreen():
                self._owner._exit_fullscreen()
            e.accept()
            return
        super().keyPressEvent(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Focal — receiver")
        self.resize(1000, 680)
        self.worker = None
        self._info = {}
        self._restart_after_stop = False
        self.settings = QSettings("PhoneCam", "PhoneCam")
        self._ensure_relay_key()
        icon_path = os.path.join(HERE, "assets", "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.bridge = Bridge()
        self.bridge.sig_ready.connect(self.on_ready)
        self.bridge.sig_state.connect(self.on_state)
        self.bridge.sig_vcam.connect(self.on_vcam)
        self.bridge.sig_log.connect(self.on_log)
        self.bridge.sig_stopped.connect(self.on_stopped)
        self.bridge.sig_stats.connect(self.on_stats)
        self.bridge.sig_relay.connect(self.on_relay)
        self.bridge.sig_obs.connect(self.on_obs)
        self.bridge.sig_meta.connect(self.on_meta)
        self.bridge.sig_cert_done.connect(self._launch_worker)
        self.bridge.sig_cert_regen.connect(self._after_regen)

        self.hooks = pcserver.Hooks(
            on_state=lambda peer, st: self.bridge.sig_state.emit(peer, st),
            on_frame=None,   # preview is the embedded browser -> skip ALL per-frame numpy work
            on_vcam=lambda ok, info: self.bridge.sig_vcam.emit(ok, info),
            on_ready=lambda info: self.bridge.sig_ready.emit(info),
            on_stats=lambda d: self.bridge.sig_stats.emit(d),
            on_relay=lambda role, c: self.bridge.sig_relay.emit(role, c),
            on_meta=lambda d: self.bridge.sig_meta.emit(d),
        )

        h = QtLogHandler(lambda m: self.bridge.sig_log.emit(m))
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logging.getLogger("phonecam").addHandler(h)

        self._build_ui()
        self._setup_shortcuts()
        QTimer.singleShot(0, self.start_server)   # auto-start

    # ---------- UI ----------
    def _section(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    def _step_row(self, index, title):
        """A numbered setup step: badge · title/url · Copy button.
        Returns (frame, badge, url_label, copy_button) so on_ready can fill the URL in."""
        row = QFrame()
        row.setObjectName("steprow")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 9, 10, 9)
        h.setSpacing(10)
        badge = QLabel(str(index))
        badge.setObjectName("badge")
        badge.setAlignment(Qt.AlignCenter)
        h.addWidget(badge, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title)
        t.setObjectName("steptitle")
        t.setWordWrap(True)
        url = QLabel("—")
        url.setObjectName("stepurl")
        url.setWordWrap(True)
        url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(t)
        col.addWidget(url)
        h.addLayout(col, 1)
        btn = QPushButton("Copy")
        btn.setObjectName("copy")
        btn.setCursor(Qt.PointingHandCursor)
        h.addWidget(btn, 0, Qt.AlignVCenter)
        return row, badge, url, btn

    def _copy(self, text, btn):
        if not text:
            return
        QApplication.clipboard().setText(text)
        btn.setText("Copied ✓")
        btn.setProperty("done", "true")
        self._restyle(btn)

        def _reset():
            btn.setText("Copy")
            btn.setProperty("done", "false")
            self._restyle(btn)
        QTimer.singleShot(1400, _reset)

    def _build_ui(self):
        # PREVIEW-FIRST: the video fills the whole window; the few live controls float on top as
        # translucent overlays; the QR code, setup steps and rarely-used tools live in a Settings
        # dialog opened from the gear button. This gives the preview the entire window.
        self.stage = QWidget()
        self.setCentralWidget(self.stage)
        lay = QVBoxLayout(self.stage)
        lay.setContentsMargins(0, 0, 0, 0)

        # Preview = embedded browser (Chromium) running the viewer page -> browser-native decode.
        self.preview = QWebEngineView()
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.setHtml(_PREVIEW_PLACEHOLDER)   # dark placeholder, never a blank white page
        self._viewer_loaded = False
        self._preview_url = ""
        self._preview_parked = False
        self.preview.loadFinished.connect(lambda ok: self._layout_overlays())  # keep overlays on top
        lay.addWidget(self.preview)

        self._urls = {"cert": "", "cam": "", "viewer": ""}
        self._settings_autoclosed = False
        self._settings_opened_once = False
        self._build_overlays()
        self._build_settings_dialog()

        # log kept in the background (not shown) so diagnostics still work without cluttering the UI
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.hide()

    def _build_overlays(self):
        """Translucent controls that float over the preview: status (top-left),
        Settings/Fullscreen (top-right), and the Start/Stop button (bottom-centre)."""
        st = self.stage
        # status cluster: server / phone / webcam dots on a translucent bar
        self.ov_status = QFrame(st)
        self.ov_status.setObjectName("overlay")
        hs = QHBoxLayout(self.ov_status)
        hs.setContentsMargins(11, 7, 12, 7)
        hs.setSpacing(11)
        self.dot_server = Dot("Server")
        self.dot_phone = Dot("Phone")
        self.dot_vcam = Dot("Webcam")
        for d in (self.dot_server, self.dot_phone, self.dot_vcam):
            hs.addWidget(d)

        # top-right: Settings + Fullscreen
        self.ov_topright = QFrame(st)
        self.ov_topright.setObjectName("overlaybar")
        ht = QHBoxLayout(self.ov_topright)
        ht.setContentsMargins(0, 0, 0, 0)
        ht.setSpacing(8)
        self.btn_settings = QPushButton("⚙  Settings")
        self.btn_settings.setObjectName("ovbtn")
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_full = QPushButton("⛶")
        self.btn_full.setObjectName("ovbtn")
        self.btn_full.setToolTip("Fullscreen (F11)")
        self.btn_full.setCursor(Qt.PointingHandCursor)
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        ht.addWidget(self.btn_settings)
        ht.addWidget(self.btn_full)

        # bottom-centre: translucent Start/Stop (controls the server)
        self.btn_start = QPushButton("▶  Start", st)
        self.btn_start.setObjectName("ovtoggle")
        self.btn_start.setProperty("running", "false")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.clicked.connect(self.toggle_server)

        for w in (self.ov_status, self.ov_topright, self.btn_start):
            w.raise_()

    def _build_settings_dialog(self):
        """The QR code, the numbered setup steps, the backend selector and the setup tools —
        everything not needed while the webcam is simply running."""
        dlg = self.dlg = _SettingsDialog(self)
        dlg.setWindowTitle("Focal — setup & tools")
        dlg.setModal(False)
        dlg.setMinimumWidth(392)
        sl = QVBoxLayout(dlg)
        sl.setContentsMargins(18, 16, 18, 16)
        sl.setSpacing(10)

        sl.addWidget(self._section("CONNECT YOUR PHONE"))
        qrcard = QFrame()
        qrcard.setObjectName("qrcard")
        qv = QVBoxLayout(qrcard)
        qv.setContentsMargins(12, 12, 12, 10)
        qv.setSpacing(7)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setFixedHeight(150)
        qv.addWidget(self.qr)
        self.qr_cap = QLabel("Scan with the iPhone camera — opens in Safari")
        self.qr_cap.setObjectName("qrcap")
        self.qr_cap.setAlignment(Qt.AlignCenter)
        qv.addWidget(self.qr_cap)
        sl.addWidget(qrcard)

        row1, self.badge_cert, self.url_cert, self.btn_copy_cert = self._step_row(1, "Install certificate")
        row2, self.badge_cam, self.url_cam, self.btn_copy_cam = self._step_row(2, "Open the camera")
        self.btn_copy_cert.clicked.connect(lambda: self._copy(self._urls.get("cert", ""), self.btn_copy_cert))
        self.btn_copy_cam.clicked.connect(lambda: self._copy(self._urls.get("cam", ""), self.btn_copy_cam))
        sl.addWidget(row1)
        sl.addWidget(row2)
        note = QLabel("Step 1 is only needed the first time you connect a phone.")
        note.setObjectName("qrcap")
        note.setWordWrap(True)
        note.setContentsMargins(2, 0, 2, 0)
        sl.addWidget(note)

        sl.addSpacing(2)
        sl.addWidget(self._section("VIRTUAL CAMERA  ·  DISCORD / ZOOM"))
        self.backend = QComboBox()
        self.backend.addItem("Auto (whatever is available)", "")
        self.backend.addItem("Unity Capture", "unitycapture")
        self.backend.addItem("OBS Virtual Camera", "obs")
        self.backend.setCurrentIndex(int(self.settings.value("backend_index", 0)))
        self.backend.currentIndexChanged.connect(self._backend_changed)
        sl.addWidget(self.backend)

        sl.addSpacing(4)
        sl.addWidget(self._section("SETUP & TOOLS"))
        tools = QGridLayout()
        tools.setHorizontalSpacing(9)
        tools.setVerticalSpacing(9)
        for i, (label, fn) in enumerate((
            ("\U0001F3AC  Set up OBS", self.setup_obs_clicked),
            ("\U0001F3A5  Unity Capture", lambda: self._run_ps("install_unitycapture.ps1")),
            ("\U0001F511  New certificate", self.regen_cert),
        )):
            b = QPushButton(label)
            b.setObjectName("tool")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            tools.addWidget(b, i // 2, i % 2)
        sl.addLayout(tools)
        dlg.adjustSize()

    def open_settings(self):
        if not getattr(self, "_dlg_positioned", False):   # centre over the window on first open only
            self._dlg_positioned = True
            self.dlg.adjustSize()
            c = self.geometry().center()
            self.dlg.move(c.x() - self.dlg.width() // 2, max(40, c.y() - self.dlg.height() // 2))
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()

    def _layout_overlays(self):
        st = getattr(self, "stage", None)
        if st is None:
            return
        W, H, m = st.width(), st.height(), 14
        self.ov_status.adjustSize()
        self.ov_status.move(m, m)
        self.ov_topright.adjustSize()
        self.ov_topright.move(max(m, W - self.ov_topright.width() - m), m)
        self.btn_start.adjustSize()
        self.btn_start.move(max(m, (W - self.btn_start.width()) // 2), H - self.btn_start.height() - 18)
        for w in (self.ov_status, self.ov_topright, self.btn_start):
            w.raise_()
            w.show()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_overlays()

    def showEvent(self, e):
        super().showEvent(e)
        self._layout_overlays()

    def _setup_shortcuts(self):
        self._sc_fs = QShortcut(QKeySequence(Qt.Key_F11), self)
        self._sc_fs.activated.connect(self.toggle_fullscreen)
        self._sc_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._sc_esc.activated.connect(self._exit_fullscreen)
        # keyboard escape hatches so the app is controllable even if an overlay ever fails to paint
        self._sc_set = QShortcut(QKeySequence("Ctrl+,"), self)
        self._sc_set.activated.connect(self.open_settings)
        self._sc_run = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._sc_run.activated.connect(self.toggle_server)

    def toggle_fullscreen(self):
        # overlays stay visible in fullscreen — they're the only controls now
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        QTimer.singleShot(0, self._layout_overlays)

    def _exit_fullscreen(self):
        if not self.isFullScreen():
            return
        self.showNormal()
        QTimer.singleShot(0, self._layout_overlays)

    def _ensure_relay_key(self):
        """Relay token (anti-hijack): without it, ANY device on the Wi-Fi could silently watch or
        hijack the camera. It rides along in the QR / camera URL (?k=...), so the phone just scans
        it — nobody types anything. Stored per-user in the registry (NOT a file), so it stays stable
        across launches (bookmarks/PWA keep working) and is never shipped in the portable build.
        Local viewers (app preview + OBS on this PC) are exempt server-side, so the preview is
        unaffected. An explicit PHONECAM_KEY env var still wins (power users / CLI)."""
        if os.environ.get("PHONECAM_KEY"):
            return
        key = self.settings.value("relay_key", "") or ""
        if not key:
            import secrets
            key = secrets.token_urlsafe(8)
            self.settings.setValue("relay_key", key)
        os.environ["PHONECAM_KEY"] = key

    # ---------- server control ----------
    def toggle_server(self):
        if self.worker and self.worker.is_alive():
            self.stop_server()
        else:
            self.start_server()

    def start_server(self):
        if self.worker and self.worker.is_alive():
            return
        if getattr(self, "_cert_generating", False):
            return                       # a keygen is already in flight -> never spawn a 2nd (races cert/key)
        cert = os.path.join(SERVER_DIR, "cert.pem")
        key = os.path.join(SERVER_DIR, "key.pem")
        if os.path.exists(cert) and os.path.exists(key):
            self._launch_worker()
            return
        # generate the cert OFF the GUI thread (2x RSA-2048 keygen ~1-3s) -> no frozen window
        self.on_log("Generating certificate…")
        self._cert_generating = True
        self.btn_start.setEnabled(False)

        def _gen():
            try:
                make_cert.main()
            except Exception as e:
                self.bridge.sig_log.emit("Certificate error: %s" % e)
            finally:
                self.bridge.sig_cert_done.emit()
        threading.Thread(target=_gen, daemon=True).start()

    @Slot()
    def _launch_worker(self):
        self._cert_generating = False
        self.btn_start.setEnabled(True)
        if self.worker and self.worker.is_alive():
            return
        cert = os.path.join(SERVER_DIR, "cert.pem")
        key = os.path.join(SERVER_DIR, "key.pem")
        if not (os.path.exists(cert) and os.path.exists(key)):
            self.on_log("Certificate missing — cannot start.")
            self._reset_idle_ui()
            return
        args = pcserver.make_args(backend=self.backend.currentData())
        self.worker = ServerWorker(args, self.hooks, self.bridge)
        self.worker.start()
        self.btn_start.setText("■  Stop")
        self.btn_start.setProperty("running", "true")
        self._restyle(self.btn_start)
        self.dot_server.set("on")

    def _reset_idle_ui(self):
        self.btn_start.setText("▶  Start")
        self.btn_start.setProperty("running", "false")
        self.btn_start.setEnabled(True)          # re-enable after the transitional teardown state
        self._restyle(self.btn_start)
        self.dot_server.set("off")
        self.dot_phone.set("off")
        self.dot_vcam.set("off")
        self._viewer_count = 0

    def stop_server(self, then_restart=False):
        # chain the restart to the actual teardown (on_stopped) -> no port race
        self._restart_after_stop = then_restart
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            # transitional state until on_stopped fires: disable the button so a click during the
            # ~200-400ms teardown can't issue a plain stop that clobbers a pending restart
            self.btn_start.setEnabled(False)
            self.btn_start.setText("↻  Restarting…" if then_restart else "■  Stopping…")
            self._restyle(self.btn_start)
            self.dot_server.set("wait")
            self.dot_phone.set("off")
            self.dot_vcam.set("off")
        else:
            self._reset_idle_ui()

    def _backup_cert(self):
        for f in ("cert.pem", "key.pem"):
            p = os.path.join(SERVER_DIR, f)
            if os.path.exists(p):
                try:
                    shutil.copy2(p, p + ".bak")
                except Exception:
                    pass

    def _backend_changed(self):
        self.settings.setValue("backend_index", self.backend.currentIndex())
        if self.worker and self.worker.is_alive():
            self.on_log("Backend switch → restarting server…")
            self.stop_server(then_restart=True)

    def regen_cert(self):
        if getattr(self, "_cert_generating", False):
            return                                # a keygen is already in flight
        was = bool(self.worker and self.worker.is_alive())
        self._backup_cert()                       # back up before overwrite (never delete without a backup)
        self.on_log("Regenerating certificate…")
        self._cert_generating = True              # block a concurrent start from reading a half-written pair

        def _gen():
            try:
                make_cert.main()
            except Exception as e:
                self.bridge.sig_log.emit("Certificate error: %s" % e)
            finally:
                self.bridge.sig_cert_regen.emit(was)
        threading.Thread(target=_gen, daemon=True).start()

    @Slot(bool)
    def _after_regen(self, was):
        self._cert_generating = False
        self.on_log("Certificate regenerated (old: *.bak).")
        if was:
            self.stop_server(then_restart=True)

    def _run_ps(self, script):
        path = os.path.join(HERE, script)
        try:
            subprocess.Popen(["powershell", "-ExecutionPolicy", "Bypass", "-File", path])
            self.on_log(f"Started: {script} (follow the UAC dialog)")
        except Exception as e:
            self.on_log(f"Error {script}: {e}")

    # ---------- GUI slots ----------
    @Slot(dict)
    def on_ready(self, info):
        self._info = info
        cam_url = info.get("cam_url", "")
        cert_url = info.get("cert_url", "")
        self._urls = {"cert": cert_url or "", "cam": cam_url or "", "viewer": info.get("viewer_url", "")}
        self.url_cam.setText(cam_url or "—")
        if cert_url:
            self.url_cert.setText(cert_url)
            self.badge_cert.setProperty("done", "false")
        else:
            self.url_cert.setText("generating certificate…")
            self.badge_cert.setProperty("done", "wait")
        self._restyle(self.badge_cert)
        self.btn_copy_cert.setEnabled(bool(cert_url))
        if cam_url:
            img = qrcode.make(cam_url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            pix = QPixmap()
            pix.loadFromData(buf.getvalue(), "PNG")
            self.qr.setPixmap(pix.scaled(142, 142, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.dot_server.set("on", f"Server ({info.get('ip', '?')})")
        # the embedded browser loads the viewer page (localhost, HTTP) -> this becomes the preview
        if not self._viewer_loaded and info.get("viewer_url"):
            local = info["viewer_url"].replace(info.get("ip", "x"), "127.0.0.1")
            self._preview_url = local
            self.preview.load(QUrl(local))
            self._viewer_loaded = True
            self._preview_parked = False
        # first time the server is ready, pop the Settings dialog so the QR is right there —
        # but only if PhoneCam is still the foreground window (don't yank focus from Discord
        # if the user launched and immediately tabbed away during the first-run cert keygen)
        if cam_url and not self._settings_opened_once:
            self._settings_opened_once = True
            if self.isActiveWindow():
                self.open_settings()

    @Slot(str, str)
    def on_state(self, peer, st):
        # status of the built-in aiortc path; browser mode is driven by relay/on_relay
        if st == "connecting":
            self.dot_phone.set("wait", "Connecting…")

    @Slot(dict)
    def on_stats(self, d):
        pass   # LIVE DATA panel removed from the UI

    @Slot(str, bool)
    def on_relay(self, role, connected):
        # browser mode: the phone (sender) and the viewers (app preview + OBS) pair up over the relay
        if role == "sender":
            self._phone_connected = connected
            if not connected:
                self._phone_batt = None
            self.dot_phone.set("on" if connected else "off", "Phone")
            self._refresh_device_label()
            # phone is streaming -> the connect QR has done its job; free the preview
            if connected and self.dlg.isVisible() and not self._settings_autoclosed:
                self._settings_autoclosed = True
                self.dlg.hide()
        elif role == "viewer":
            # external viewer count only — the "Webcam" dot is owned SOLELY by on_vcam/on_obs
            # (the real virtual-camera lifecycle), so app-preview/OBS viewer churn (e.g. the
            # minimize-park/restore) can't clobber a genuine "No webcam" failure back to green.
            self._viewer_count = max(0, getattr(self, "_viewer_count", 0) + (1 if connected else -1))

    @Slot(dict)
    def on_meta(self, d):
        b = (d or {}).get("battery")
        if isinstance(b, dict) and "level" in b:
            self._phone_batt = b
            self._refresh_device_label()

    def _refresh_device_label(self):
        pass   # device/battery line removed from the UI

    def setup_obs_clicked(self):
        url = self._info.get("viewer_url")
        if not url:
            self.on_log("Start the server first (green Start).")
            return
        pw = self.settings.value("obs_password", "") or None
        self.on_log("Setting up OBS… (is OBS running? is the WebSocket server enabled?)")

        def work():
            try:
                obs_control.setup_obs(url, password=pw)
                self.bridge.sig_obs.emit(True, "OBS ready: browser source + virtual camera started. "
                                               "In Discord choose: OBS Virtual Camera.")
            except PermissionError as e:
                self.bridge.sig_obs.emit(False, "PW:" + str(e))
            except Exception as e:
                self.bridge.sig_obs.emit(False, str(e))

        threading.Thread(target=work, daemon=True).start()

    @Slot(bool, str)
    def on_obs(self, ok, msg):
        if ok:
            self.on_log(msg)
            self.dot_vcam.set("on", "Webcam ready")
            return
        if msg.startswith("PW:"):
            from PySide6.QtWidgets import QInputDialog
            pw, okk = QInputDialog.getText(self, "OBS password",
                "OBS WebSocket password\n(OBS → Tools → WebSocket Server Settings → Show Connect Info):")
            if okk and pw:
                self.settings.setValue("obs_password", pw)
                self.setup_obs_clicked()
            else:
                self.on_log(msg[3:])
        else:
            self.on_log("OBS: " + msg)

    @Slot(bool, str)
    def on_vcam(self, ok, info):
        if ok:
            self.dot_vcam.set("on", "Webcam ready")
            self.on_log(f"Virtual camera: {info}")
        else:
            self.dot_vcam.set("bad", "No webcam")
            self.on_log("No virtual camera — install Unity Capture (Settings ▸ Setup & tools).")

    @Slot(str)
    def on_log(self, msg):
        self.log.appendPlainText(msg)

    @Slot(str)
    def on_stopped(self, reason):
        if reason:
            self.on_log(reason)
            low = reason.lower()
            if ("10048" in reason) or ("only one usage" in low) or ("bind" in low and "address" in low):
                self.on_log("→ Port 8443/8080 is busy — another Focal is already running. "
                            "Close the other window (or end the leftover 'pythonw' in Task Manager), then press Start.")
                if not self._viewer_loaded:
                    self.preview.setHtml(_PREVIEW_PORT_BUSY)
        if self.worker:                      # the worker has fully stopped -> cleanup
            self.worker.join(timeout=2)
            self.worker = None
        if self._restart_after_stop:         # restart after backend switch / cert regen
            self._restart_after_stop = False
            QTimer.singleShot(150, self.start_server)   # keep the "Restarting…" state until it relaunches
        else:
            self._reset_idle_ui()

    def changeEvent(self, e):
        # While minimized nobody sees the preview, yet its WebRTC session keeps the PHONE
        # encoding a whole extra stream (and this PC decoding + compositing it). Park the
        # embedded viewer on minimize and reconnect on restore — the vcam bridge that feeds
        # Discord is a separate connection and is unaffected.
        if e.type() == QEvent.WindowStateChange and self._viewer_loaded:
            if self.isMinimized():
                if not self._preview_parked:
                    self._preview_parked = True
                    self.preview.setHtml(_PREVIEW_PLACEHOLDER)
            elif self._preview_parked:
                self._preview_parked = False
                self.preview.load(QUrl(self._preview_url))
        super().changeEvent(e)

    def _restyle(self, w):
        w.style().unpolish(w)
        w.style().polish(w)

    def closeEvent(self, e):
        # briefly wait for the worker's teardown so serve()'s finally can withdraw the mDNS
        # record and close the runners cleanly (daemon thread would otherwise be killed mid-teardown)
        self.stop_server()
        if self.worker:
            try:
                self.worker.join(timeout=2)
            except Exception:
                pass
        e.accept()


_PREVIEW_PLACEHOLDER = (
    "<!doctype html><html><body style='margin:0;height:100vh;display:flex;align-items:center;"
    "justify-content:center;background:#0e1116;color:#8b97a6;font-family:Segoe UI,sans-serif;"
    "font-size:15px'><div>Starting the server&hellip;</div></body></html>"
)
_PREVIEW_PORT_BUSY = (
    "<!doctype html><html><body style='margin:0;height:100vh;display:flex;align-items:center;"
    "justify-content:center;text-align:center;background:#0e1116;color:#e6edf3;"
    "font-family:Segoe UI,sans-serif'><div style='max-width:420px;padding:24px'>"
    "<div style='font-size:34px'>&#9888;&#65039;</div>"
    "<div style='font-size:17px;font-weight:700;margin:10px 0 6px'>Port already in use</div>"
    "<div style='font-size:13px;color:#8b97a6;line-height:1.5'>Another Focal is already running and "
    "holding port 8443/8080. Close the other window, then press <b style='color:#3fb950'>Start</b>."
    "</div></div></body></html>"
)

_SINGLETON_SOCK = None


def _acquire_single_instance(port=8442):
    """Bind a loopback sentinel port, held for the whole process lifetime. If the bind fails,
    another PhoneCam is already running -> the caller shows a message and exits, instead of
    starting a SECOND server that fails to bind on 8443/8080 and leaves the preview blank/white.
    The OS frees the socket when the process dies, so there is no stale-lock problem."""
    global _SINGLETON_SOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _SINGLETON_SOCK = s
        return True
    except OSError:
        s.close()
        return False


def main():
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)   # recommended for QtWebEngine
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    if not _acquire_single_instance():                     # only ONE PhoneCam at a time
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(QMessageBox.Information, "Focal",
            "Focal is already running.\n\nUse the window that's already open. If you can't find it, "
            "an old copy may be stuck — open Task Manager, end the 'pythonw.exe' process, then start again.")
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)   # don't hide behind other windows (pythonw)
        box.exec()
        return
    icon = os.path.join(HERE, "assets", "icon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    w = MainWindow()
    w.show()
    # never let the window exceed the visible screen (covers HiDPI display scaling)
    scr = app.primaryScreen().availableGeometry()
    fg = w.frameGeometry()
    if fg.height() > scr.height() or fg.width() > scr.width():
        w.resize(min(w.width(), scr.width() - 40), min(w.height(), scr.height() - 80))
        w.move(scr.left() + 20, scr.top() + 20)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
