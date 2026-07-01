"""
PhoneCam — standalone receiver app (GUI).

A single-window application that:
  - runs the built-in server (HTTPS + WS signaling + aiortc WebRTC receiver),
  - shows a live preview of the phone's image,
  - displays status + live stats (server / phone / camera, resolution/fps/bitrate),
  - gives a QR code + URL for the phone,
  - one-click: firewall, Unity Capture install, certificate regen, fullscreen.

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
import time

import numpy as np

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer, QSettings, QUrl
from PySide6.QtGui import QImage, QPixmap, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox,
    QHBoxLayout, QVBoxLayout, QGridLayout, QFrame, QPlainTextEdit, QSizePolicy,
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
BG, PANEL, LINE, TXT, MUT = "#0e1116", "#161b22", "#222b36", "#e6edf3", "#8b97a6"
ACC, OK, WARN, BAD = "#2ea043", "#3fb950", "#d29922", "#f85149"
INSET = "#0b0e13"

STYLE = f"""
QMainWindow, QWidget {{ background:{BG}; color:{TXT};
  font-family:'Segoe UI',sans-serif; font-size:13px; }}
QFrame#panel {{ background:{PANEL}; border:1px solid {LINE}; border-radius:14px; }}
QFrame#qrcard, QFrame#devcard, QFrame#statcell {{ background:{INSET};
  border:1px solid {LINE}; border-radius:12px; }}
QLabel#preview {{ background:#000; border:1px solid {LINE}; border-radius:14px; color:{MUT};
  font-size:15px; }}
QLabel#rec {{ background:rgba(0,0,0,.55); border-radius:8px; padding:3px 9px;
  font-size:12px; font-weight:700; color:#fff; }}
QPushButton {{ background:{PANEL}; border:1px solid {LINE}; border-radius:9px;
  padding:9px 12px; color:{TXT}; font-weight:600; }}
QPushButton:hover {{ border-color:{ACC}; }}
QPushButton#primary {{ background:{ACC}; border:1px solid {OK}; color:#fff;
  padding:11px; font-size:14px; border-radius:11px; }}
QPushButton#primary:hover {{ background:{OK}; }}
QPushButton#primary[running="true"] {{ background:#21262d; border-color:{BAD}; color:{BAD}; }}
QPushButton#linkbtn {{ background:transparent; border:none; color:{MUT}; font-weight:600;
  padding:2px 6px; }}
QPushButton#linkbtn:hover {{ color:{TXT}; border:none; }}
QComboBox {{ background:{PANEL}; border:1px solid {LINE}; border-radius:9px; padding:8px; }}
QComboBox QAbstractItemView {{ background:{PANEL}; border:1px solid {LINE};
  selection-background-color:{ACC}; }}
QPlainTextEdit {{ background:{INSET}; border:1px solid {LINE}; border-radius:10px;
  color:{MUT}; font-family:Consolas,monospace; font-size:11px; padding:6px; }}
QLabel#h1 {{ font-size:18px; font-weight:700; }}
QLabel#sub {{ color:{MUT}; font-size:11px; }}
QLabel#chip {{ background:{PANEL}; border:1px solid {LINE}; border-radius:999px;
  padding:5px 12px; font-size:12px; font-weight:600; }}
QLabel#chip[state="on"]   {{ background:rgba(63,185,80,.10);  border-color:rgba(63,185,80,.35); }}
QLabel#chip[state="wait"] {{ background:rgba(210,153,34,.10); border-color:rgba(210,153,34,.35); }}
QLabel#chip[state="bad"]  {{ background:rgba(248,81,73,.10);  border-color:rgba(248,81,73,.35); }}
QLabel#section {{ color:{MUT}; font-size:11px; font-weight:700; letter-spacing:.6px; }}
QLabel#url {{ color:{MUT}; font-family:Consolas,monospace; font-size:11px; }}
QLabel#devlbl {{ color:{MUT}; font-size:11px; }}
QLabel#statval {{ font-size:15px; font-weight:700; color:{TXT}; }}
QLabel#statcap {{ font-size:10px; color:{MUT}; }}
QFrame#sep {{ background:{LINE}; max-height:1px; min-height:1px; border:none; }}
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
        ip = pcserver.lan_ip()
        ssl_ctx = pcserver.build_ssl(self.args.cert, self.args.key)
        await pcserver.serve(self.args, ssl_ctx, ip, self.hooks, self.stop_event)
        self.bridge.sig_stopped.emit("")

    def stop(self):
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhoneCam — receiver")
        self.resize(1000, 560)
        self.worker = None
        self._last_emit = 0.0
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
        self.bridge.sig_frame.connect(self.on_frame)
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
            on_frame=self._on_frame_worker,
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

    def _statcell(self, caption):
        cell = QFrame()
        cell.setObjectName("statcell")
        v = QVBoxLayout(cell)
        v.setContentsMargins(8, 7, 8, 7)
        v.setSpacing(1)
        val = QLabel("—")
        val.setObjectName("statval")
        cap = QLabel(caption)
        cap.setObjectName("statcap")
        v.addWidget(val)
        v.addWidget(cap)
        return cell, val, cap

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        # ---- header: title + subtitle | status chips ----
        self._head = QWidget()
        head = QHBoxLayout(self._head)
        head.setContentsMargins(0, 0, 0, 0)
        titlebox = QVBoxLayout()
        titlebox.setSpacing(0)
        title = QLabel("📱 PhoneCam")
        title.setObjectName("h1")
        sub = QLabel("Phone → virtual webcam")
        sub.setObjectName("sub")
        titlebox.addWidget(title)
        titlebox.addWidget(sub)
        head.addLayout(titlebox)
        head.addStretch()
        self.dot_server = Dot("Server")
        self.dot_phone = Dot("Phone")
        self.dot_vcam = Dot("Camera")
        for d in (self.dot_server, self.dot_phone, self.dot_vcam):
            head.addWidget(d)
            head.addSpacing(8)
        root.addWidget(self._head)

        # ---- middle: preview | right panel ----
        mid = QHBoxLayout()
        mid.setSpacing(14)

        # Preview = embedded browser (Chromium) running the viewer page ->
        # browser-native (hardware) decode, good fps. aiortc is NOT involved here.
        self.preview = QWebEngineView()
        self.preview.setMinimumSize(440, 280)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.setHtml(_PREVIEW_PLACEHOLDER)   # dark placeholder, never a blank white page
        self._viewer_loaded = False
        mid.addWidget(self.preview, 1)

        side = self._side = QFrame()
        side.setObjectName("panel")
        side.setFixedWidth(320)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 14, 14, 14)
        sl.setSpacing(9)

        # PHONE
        sl.addWidget(self._section("PHONE"))
        qrcard = QFrame()
        qrcard.setObjectName("qrcard")
        qv = QVBoxLayout(qrcard)
        qv.setContentsMargins(12, 12, 12, 12)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setFixedHeight(150)
        qv.addWidget(self.qr)
        sl.addWidget(qrcard)

        devcard = QFrame()
        devcard.setObjectName("devcard")
        dv = QVBoxLayout(devcard)
        dv.setContentsMargins(12, 10, 12, 10)
        dv.setSpacing(4)
        self.url_cam = QLabel("—")
        self.url_cam.setObjectName("url")
        self.url_cam.setWordWrap(True)
        self.url_cam.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_cert = QLabel("")
        self.url_cert.setObjectName("url")
        self.url_cert.setWordWrap(True)
        self.url_cert.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_viewer = QLabel("")
        self.url_viewer.setObjectName("url")
        self.url_viewer.setWordWrap(True)
        self.url_viewer.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dv.addWidget(self.url_cam)
        dv.addWidget(self.url_viewer)
        dv.addWidget(self.url_cert)
        sl.addWidget(devcard)

        # VIRTUAL CAMERA
        sl.addSpacing(4)
        sl.addWidget(self._section("VIRTUAL CAMERA"))
        self.backend = QComboBox()
        self.backend.addItem("Auto (whatever is available)", "")
        self.backend.addItem("Unity Capture", "unitycapture")
        self.backend.addItem("OBS Virtual Camera", "obs")
        self.backend.setCurrentIndex(int(self.settings.value("backend_index", 0)))
        self.backend.currentIndexChanged.connect(self._backend_changed)
        sl.addWidget(self.backend)

        self.btn_start = QPushButton("▶ Start")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.toggle_server)
        sl.addWidget(self.btn_start)

        sl.addStretch()
        sep = QFrame()
        sep.setObjectName("sep")
        sl.addWidget(sep)

        for label, fn in (
            ("🎬 OBS browser mode (better fps)", self.setup_obs_clicked),
            ("🎥 Install Unity Capture", lambda: self._run_ps("install_unitycapture.ps1")),
            ("🔑 Regenerate certificate", self.regen_cert),
            ("⛶ Fullscreen (F11)", self.toggle_fullscreen),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            sl.addWidget(b)

        mid.addWidget(side)
        root.addLayout(mid, 1)

        # log kept in the background (not shown) so diagnostics still work without cluttering the UI
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.hide()

    def _setup_shortcuts(self):
        self._sc_fs = QShortcut(QKeySequence(Qt.Key_F11), self)
        self._sc_fs.activated.connect(self.toggle_fullscreen)
        self._sc_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._sc_esc.activated.connect(self._exit_fullscreen)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self._exit_fullscreen()
        else:
            self._head.hide(); self._side.hide()
            self.showFullScreen()

    def _exit_fullscreen(self):
        if not self.isFullScreen():
            return
        self._head.show(); self._side.show()
        self.showNormal()

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
        cert = os.path.join(SERVER_DIR, "cert.pem")
        key = os.path.join(SERVER_DIR, "key.pem")
        if os.path.exists(cert) and os.path.exists(key):
            self._launch_worker()
            return
        # generate the cert OFF the GUI thread (2x RSA-2048 keygen ~1-3s) -> no frozen window
        self.on_log("Generating certificate…")
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
        self.btn_start.setText("■ Stop")
        self.btn_start.setProperty("running", "true")
        self._restyle(self.btn_start)
        self.dot_server.set("on")

    def _reset_idle_ui(self):
        self.btn_start.setText("▶ Start")
        self.btn_start.setProperty("running", "false")
        self._restyle(self.btn_start)
        self.dot_server.set("off")
        self.dot_phone.set("off")
        self.dot_vcam.set("off")
        self._viewer_count = 0

    def stop_server(self, then_restart=False):
        # chain the restart to the actual teardown (on_stopped) -> no port race
        self._restart_after_stop = then_restart
        if self.worker:
            self.worker.stop()
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
        was = bool(self.worker and self.worker.is_alive())
        self._backup_cert()                       # back up before overwrite (never delete without a backup)
        self.on_log("Regenerating certificate…")

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

    # ---------- worker callback (other thread) ----------
    def _on_frame_worker(self, rgb):
        if rgb is None:
            self.bridge.sig_frame.emit(None)
            return
        now = time.monotonic()
        if now - self._last_emit < 1 / 15:
            return
        self._last_emit = now
        self.bridge.sig_frame.emit(np.ascontiguousarray(rgb[::2, ::2]))

    # ---------- GUI slots ----------
    @Slot(dict)
    def on_ready(self, info):
        self._info = info
        cam_url = info.get("cam_url", "")
        self.url_cam.setText("2. Camera (scan the QR or open):  " + cam_url)
        self.url_viewer.setText("OBS source:  " + info.get("viewer_url", ""))
        self.url_cert.setText(("1. Setup — open this on the phone first:  " + info["cert_url"]) if info.get("cert_url")
                              else "Certificate: (no CA yet)")
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
            self.preview.load(QUrl(local))
            self._viewer_loaded = True

    @Slot(str, str)
    def on_state(self, peer, st):
        # status of the built-in aiortc path; browser mode is driven by relay/on_relay
        if st == "connecting":
            self.dot_phone.set("wait", "Connecting…")

    @Slot(object)
    def on_frame(self, rgb):
        pass   # the preview is the embedded browser (QWebEngineView); aiortc drawing no longer needed

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
        elif role == "viewer":
            self._viewer_count = max(0, getattr(self, "_viewer_count", 0) + (1 if connected else -1))
            n = self._viewer_count
            self.dot_vcam.set("on" if n else "off", f"Viewer ({n})" if n else "Viewer")

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
            self.dot_vcam.set("on", "OBS vcam")
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
            self.dot_vcam.set("on", "Camera")
            self.on_log(f"Virtual camera: {info}")
        else:
            self.dot_vcam.set("bad", "No vcam")
            self.on_log("No virtual camera — install Unity Capture (button on the right).")

    @Slot(str)
    def on_log(self, msg):
        self.log.appendPlainText(msg)

    @Slot(str)
    def on_stopped(self, reason):
        if reason:
            self.on_log(reason)
            low = reason.lower()
            if ("10048" in reason) or ("only one usage" in low) or ("bind" in low and "address" in low):
                self.on_log("→ Port 8443/8080 is busy — another PhoneCam is already running. "
                            "Close the other window (or end the leftover 'pythonw' in Task Manager), then press Start.")
                if not self._viewer_loaded:
                    self.preview.setHtml(_PREVIEW_PORT_BUSY)
        if self.worker:                      # the worker has fully stopped -> cleanup
            self.worker.join(timeout=2)
            self.worker = None
        self._reset_idle_ui()
        if self._restart_after_stop:         # restart after backend switch / cert regen
            self._restart_after_stop = False
            QTimer.singleShot(150, self.start_server)

    def _restyle(self, w):
        w.style().unpolish(w)
        w.style().polish(w)

    def closeEvent(self, e):
        self.stop_server()
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
    "<div style='font-size:13px;color:#8b97a6;line-height:1.5'>Another PhoneCam is already running and "
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
        box = QMessageBox(QMessageBox.Information, "PhoneCam",
            "PhoneCam is already running.\n\nUse the window that's already open. If you can't find it, "
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
