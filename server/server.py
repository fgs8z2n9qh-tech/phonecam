"""
PhoneCam server — receives the phone's camera feed over WebRTC and forwards it
to a virtual webcam (Unity Capture) that Discord etc. can see.

Flow:
  phone browser (getUserMedia + WebRTC, HTTPS)
    -> aiohttp HTTPS + WebSocket signaling
    -> aiortc RTCPeerConnection (receiver / answerer)
    -> every incoming video frame -> pyvirtualcam -> Unity Capture vcam
    -> Discord: pick the "Unity Video Capture" camera

Run:  python server/server.py
Generate the certificate first:  python server/make_cert.py
"""
import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import ssl
from urllib.parse import urlparse

import numpy as np
from aiohttp import web, WSMsgType
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.sdp import candidate_from_sdp

# --- LAN: "tell" the phone a high REMB so it doesn't throttle itself ---
# aiortc video has ONLY goog-remb feedback (no transport-cc), so REMB is the
# only bandwidth signal. The estimator starts from the MEASURED incoming bitrate and
# creeps up slowly -> the phone stays low (blurry image). Fix: override the REMB value
# to a high number so the phone dares to ramp up to maxBitrate. (Disabling it was worse:
# zero signal.)
import aiortc.rtcrtpreceiver as _aiortc_recv
from aiortc.rate import RemoteBitrateEstimator as _RealBitrateEstimator

_FORCE_REMB_BPS = 10_000_000  # 10 Mbps REMB ceiling (the real limit is the phone's maxBitrate)


class _ForcedHighBitrateEstimator(_RealBitrateEstimator):
    def add(self, *args, **kwargs):
        result = super().add(*args, **kwargs)  # ssrc/timing stay correct
        if result is not None:
            _, ssrcs = result
            return (_FORCE_REMB_BPS, ssrcs)     # but report back a high bitrate
        return result


_aiortc_recv.RemoteBitrateEstimator = _ForcedHighBitrateEstimator

# --- ARTIFACT-FIX 1: larger UDP receive buffer (the main cause of green corruption) ---
# aioice only sets a 256 KB SO_RCVBUF; a 720p keyframe burst can overflow this
# -> the kernel drops packets -> corrupted (green) frames. Raise it to 16 MB.
_UDP_RCVBUF = 16 * 1024 * 1024
try:
    import aioice.turn as _aioice_turn
    import aioice.ice as _aioice_ice

    _aioice_turn.UDP_SOCKET_BUFFER_SIZE = _UDP_RCVBUF
    if hasattr(_aioice_ice, "turn"):
        _aioice_ice.turn.UDP_SOCKET_BUFFER_SIZE = _UDP_RCVBUF

    _orig_connection_made = _aioice_ice.StunProtocol.connection_made

    def _patched_connection_made(self, transport):
        _orig_connection_made(self, transport)
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _UDP_RCVBUF)
                granted = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                logging.getLogger("phonecam").info(
                    "UDP SO_RCVBUF: requested=%d granted=%d", _UDP_RCVBUF, granted)
            except OSError as e:
                logging.getLogger("phonecam").warning("Failed to set SO_RCVBUF: %s", e)

    _aioice_ice.StunProtocol.connection_made = _patched_connection_made
except Exception as _e:
    logging.getLogger("phonecam").warning("UDP buffer patch skipped: %s", _e)

# --- ARTIFACT-FIX 2: larger video jitter buffer (128 -> 256 packets) ---
# At higher bitrates a keyframe can exceed 128 packets, so the default buffer
# can't hold even a single keyframe -> poor recovery. 256 (power of two) gives headroom.
try:
    from aiortc.jitterbuffer import JitterBuffer as _JitterBuffer

    _orig_recv_init = _aiortc_recv.RTCRtpReceiver.__init__

    def _patched_recv_init(self, kind, transport, *args, **kwargs):
        _orig_recv_init(self, kind, transport, *args, **kwargs)
        if kind == "video":
            setattr(self, "_RTCRtpReceiver__jitter_buffer",
                    _JitterBuffer(capacity=256, is_video=True))

    _aiortc_recv.RTCRtpReceiver.__init__ = _patched_recv_init
except Exception as _e:
    logging.getLogger("phonecam").warning("jitter buffer patch skipped: %s", _e)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("phonecam")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB_DIR = os.path.join(ROOT, "web")

# Fixed resolution/fps of the virtual camera (the incoming image is resized to this).
VCAM_W, VCAM_H, VCAM_FPS = 1280, 720, 30

# The video b=AS ceiling in kbps (for the aiortc fallback path). On the browser path the
# phone's slider (setParameters maxBitrate) is the real control; with a browser viewer there
# is no artifact risk at high bitrates, so the ceiling is more generous.
MAX_KBPS = 20000


def _fit_vcam(w, h, max_long=1920, max_short=1080):
    """Even (w, h) that fits a 1920x1080 box, keeping orientation + aspect ratio.
    Keeps the phone's NATIVE resolution when it's already <=1080p; only caps higher feeds."""
    long_, short_ = max(w, h), min(w, h)
    scale = min(1.0, max_long / long_, max_short / short_)
    nw = max(2, (int(round(w * scale)) // 2) * 2)
    nh = max(2, (int(round(h * scale)) // 2) * 2)
    return nw, nh


class VCamSink:
    """Starts the virtual camera lazily on the first frame, so the server
    still starts even if Unity Capture isn't installed yet."""

    def __init__(self, width, height, fps, backend, on_vcam=None):
        self.width, self.height, self.fps, self.backend = width, height, fps, backend
        self.cam = None
        self._failed = False
        self.on_vcam = on_vcam  # callback(ok: bool, info: str)

    def configure(self, width, height, fps):
        """Set the vcam dimensions BEFORE it's lazily created (from the first real frame),
        so the virtual camera matches the phone's native resolution/fps instead of a fixed 720p30."""
        if self.cam is None:
            self.width, self.height, self.fps = int(width), int(height), int(fps)

    def send_rgb(self, rgb):
        if self._failed:
            return
        if self.cam is None:
            try:
                import pyvirtualcam
                kwargs = dict(width=self.width, height=self.height, fps=self.fps,
                              fmt=pyvirtualcam.PixelFormat.RGB)
                if self.backend:
                    kwargs["backend"] = self.backend
                self.cam = pyvirtualcam.Camera(**kwargs)
                log.info("Virtual camera live: %s  %dx%d@%d  (backend=%s)",
                         self.cam.device, self.width, self.height, self.fps,
                         self.backend or "auto")
                if self.on_vcam:
                    self.on_vcam(True, self.cam.device)
            except Exception as e:
                self._failed = True
                log.error("Virtual camera did not start: %s", e)
                log.error("Install Unity Capture (see README) then start again.")
                if self.on_vcam:
                    self.on_vcam(False, str(e))
                return
        try:
            self.cam.send(rgb)   # NO sleep_until_next_frame: otherwise frames pile up -> big lag
        except Exception as e:
            log.error("vcam.send error: %s", e)

    def close(self):
        if self.cam is not None:
            try:
                self.cam.close()
            except Exception:
                pass
            self.cam = None


async def consume_video(track, sink, loop, hooks=None):
    """Places incoming frames onto the vcam's fixed canvas with the CORRECT ASPECT RATIO
    (letterbox/pillarbox) -> a portrait image is NOT stretched, it gets black bars."""
    log.info("Started receiving video track.")
    sink._failed = False   # new stream -> the vcam can retry if it failed before
    W = H = None           # decided from the FIRST frame (native res, capped to 1080p)
    canvas = None
    last = None            # last (nw, nh) -> for redrawing the bars
    frames = 0
    stat_t0 = loop.time()  # real statistics (incoming resolution + fps)
    stat_n = 0
    while True:
        try:
            frame = await track.recv()
        except Exception:
            break
        try:
            w, h = frame.width, frame.height
            if not w or not h:
                continue
            if canvas is None:                              # first frame: size the vcam to it
                W, H = _fit_vcam(w, h)
                # >=60fps so a 60fps phone feed isn't capped to 30 by the vcam; native res so a
                # 1080p feed isn't pre-downscaled to 720p before Discord/OBS ever see it.
                sink.configure(W, H, max(sink.fps, 60))
                canvas = np.zeros((H, W, 3), dtype=np.uint8)
                log.info("vcam sized to first frame: %dx%d", W, H)
            scale = min(W / w, H / h)                       # fit inward, keep aspect ratio
            nw = max(2, min(W, (int(round(w * scale)) // 2) * 2))   # even size for swscale
            nh = max(2, min(H, (int(round(h * scale)) // 2) * 2))
            small = frame.reformat(width=nw, height=nh, format="rgb24").to_ndarray()
            if last != (nw, nh):                           # orientation changed -> clear bars to black
                canvas[:] = 0
                last = (nw, nh)
            y0, x0 = (H - nh) // 2, (W - nw) // 2
            canvas[y0:y0 + nh, x0:x0 + nw] = small         # image centered
        except Exception as e:
            log.error("frame conversion error: %s", e)
            continue
        if hooks and hooks.on_frame:
            try:
                hooks.on_frame(canvas)  # GUI preview (the hook makes its own copy)
            except Exception:
                pass
        # sending may block -> run in executor; wait before overwriting the canvas
        await loop.run_in_executor(None, sink.send_rgb, canvas)
        frames += 1
        if frames == 1:
            log.info("First frame forwarded to the virtual camera.")
        stat_n += 1
        dt = loop.time() - stat_t0
        if dt >= 2.0:
            fps = stat_n / dt
            log.info("STATS  incoming: %dx%d  ~%.1f fps", w, h, fps)
            if hooks and hooks.on_stats:
                try:
                    hooks.on_stats({"res": "%d×%d" % (w, h), "fps": round(fps, 1)})
                except Exception:
                    pass
            stat_t0 = loop.time()
            stat_n = 0
    log.info("Video track ended.")
    if hooks and hooks.on_frame:
        try:
            hooks.on_frame(None)  # signal to the preview: stream ended
        except Exception:
            pass


async def vcam_bridge(http_port, sink, hooks, stop_event):
    """Internal aiortc viewer that bridges the browser relay stream -> the virtual camera.

    The phone sends ONE WebRTC stream to the relay, which fans it out to all viewers. The app's
    on-screen preview is one viewer (a hardware-decoding browser); THIS is a second, headless
    viewer that software-decodes the H.264 with aiortc and pushes the frames into pyvirtualcam
    (Unity Capture / OBS vcam). That makes Discord's "Unity Video Capture" work WITHOUT OBS —
    the app's original no-OBS promise. Joins like any other viewer, auto-reconnects, and is exempt
    from the relay key because it connects from localhost."""
    import aiohttp
    loop = asyncio.get_running_loop()
    url = "http://127.0.0.1:%d/relay" % http_port
    while not (stop_event is not None and stop_event.is_set()):
        pc = None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(url, heartbeat=20) as ws:
                    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))

                    @pc.on("track")
                    def on_track(track):
                        if track.kind == "video":
                            asyncio.ensure_future(consume_video(track, sink, loop, hooks))

                    await ws.send_json({"type": "hello", "role": "viewer"})
                    log.info("vcam bridge: joined relay as internal viewer (feeds the virtual camera)")
                    async for msg in ws:
                        if msg.type != WSMsgType.TEXT:
                            if msg.type == WSMsgType.ERROR:
                                break
                            continue
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        t = data.get("type")
                        if t == "offer":
                            await pc.setRemoteDescription(
                                RTCSessionDescription(sdp=data["sdp"], type="offer"))
                            await pc.setLocalDescription(await pc.createAnswer())
                            await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})
                        elif t == "ice":
                            c = data.get("candidate") or {}
                            cstr = c.get("candidate")
                            if cstr:
                                try:
                                    cand = candidate_from_sdp(
                                        cstr.split(":", 1)[1] if cstr.startswith("candidate:") else cstr)
                                    cand.sdpMid = c.get("sdpMid")
                                    cand.sdpMLineIndex = c.get("sdpMLineIndex")
                                    await pc.addIceCandidate(cand)
                                except Exception as e:
                                    log.debug("bridge ice skipped: %s", e)
                        elif t == "peer-gone":
                            break   # phone left -> tear down, then reconnect and wait
        except asyncio.CancelledError:
            if pc is not None:
                try:
                    await pc.close()
                except Exception:
                    pass
            raise
        except Exception as e:
            log.debug("vcam bridge: %s", e)
        finally:
            if pc is not None:
                try:
                    await pc.close()
                except Exception:
                    pass
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(1.5)


_NOCACHE = {"Cache-Control": "no-store, must-revalidate"}  # the phone always gets the fresh page


async def index(request):
    return web.FileResponse(os.path.join(WEB_DIR, "index.html"), headers=_NOCACHE)


def _is_local(peer):
    """Whether the viewer runs on this same machine (app preview / OBS) -> allowed without a key."""
    if not peer:
        return False
    p = str(peer)
    return p.startswith("127.") or p in ("::1", "localhost") or p.startswith("::ffff:127.")


def _host_only(hostport):
    """Strip the :port from a Host header, handling IPv6 literals ([::1]:8080)."""
    if not hostport:
        return ""
    h = hostport
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h.strip("[]")
    return h.rsplit(":", 1)[0] if ":" in h else h


def _origin_ok(request):
    """Reject a cross-origin WebSocket upgrade (browser drive-by camera leak): a malicious web
    page could open ws://<lan-ip>:port/relay as a viewer. Browsers always send an Origin header
    on a WS handshake and JS cannot forge it, so requiring Origin's host == the server's host
    blocks that. Non-browser clients (no Origin) pass here and are gated by the relay key instead."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        oh = urlparse(origin).hostname
    except Exception:
        return False
    host = _host_only(request.host)
    if oh and host and oh == host:
        return True
    local = {"127.0.0.1", "::1", "localhost"}
    return (oh in local) and (host in local)


def _relay_key():
    """Relay key (anti-hijack) — OFF BY DEFAULT (empty = no requirement, the usual mode).
    Only takes effect if the user explicitly sets one: the PHONECAM_KEY environment variable.
    This way plain LAN use is never disturbed; whoever wants a protected relay opts in."""
    return os.environ.get("PHONECAM_KEY", "").strip()


def _ice_servers():
    """ICE servers for the phone/viewer. Default: public STUN (not even needed on a LAN).
    With TURN, internet mode also works: PHONECAM_TURN='turn:host:3478|user|pass' (| separator)."""
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    turn = os.environ.get("PHONECAM_TURN", "").strip()
    if turn:
        parts = turn.split("|")
        entry = {"urls": parts[0]}
        if len(parts) >= 3:
            entry["username"], entry["credential"] = parts[1], parts[2]
        servers.append(entry)
    return servers


async def ice_config(request):
    return web.json_response({"iceServers": _ice_servers()}, headers=_NOCACHE)


_MANIFEST = {
    "name": "PhoneCam", "short_name": "PhoneCam", "display": "standalone",
    "orientation": "any", "background_color": "#0e1116", "theme_color": "#0e1116",
    "scope": "/",
}


async def manifest_handler(request):
    # start_url with the relay key -> the "Add to Home Screen" icon also starts as an authorized sender
    m = dict(_MANIFEST)
    key = request.app.get("relay", {}).get("key", "")
    m["start_url"] = ("/?k=" + key) if key else "/"
    return web.json_response(m, headers=_NOCACHE, content_type="application/manifest+json")


async def health(request):
    return web.json_response({"ok": True})


async def on_shutdown(app):
    for pc in list(app["pcs"]):
        await pc.close()
    app["pcs"].clear()
    app["sink"].close()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def start_mdns(ip, port):
    """Best-effort: publish 'phonecam.local' over mDNS (iOS resolves it) so you don't have to
    type a changing IP. The 'zeroconf' package is optional; if it's missing, we skip silently.
    NOTE: for the HTTPS camera page the certificate must include the 'phonecam.local'
    SAN (make_cert.py already adds it -> regenerate once). The IP works regardless."""
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except Exception:
        return None
    try:
        zc = Zeroconf()
        info = ServiceInfo(
            "_https._tcp.local.",
            "PhoneCam._https._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=int(port),
            server="phonecam.local.",
            properties={},
        )
        zc.register_service(info)
        log.info("mDNS: phonecam.local -> %s published", ip)
        return (zc, info)
    except Exception as e:
        log.info("mDNS skipped: %s", e)
        return None


CA_DER = os.path.join(HERE, "phonecam-ca.cer")

# Simple HTTP page (no cert) where the iPhone can download/install the CA with
# a single tap. (There's no AirDrop on Windows, so this is the most convenient.)
CERT_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name=theme-color content="#0e1116">
<title>PhoneCam - Setup</title>
<style>
:root{--bg:#0e1116;--card:#161b22;--line:#222b36;--txt:#e6edf3;--mut:#8b97a6;--acc:#3fb950;--acc2:#2ea043;--warn:#d29922}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
 background:radial-gradient(130% 90% at 50% -10%,#1b2231,#0d1117 60%);color:var(--txt);margin:0;
 padding:calc(env(safe-area-inset-top,0px) + 22px) 18px 44px;line-height:1.5}
.wrap{max-width:470px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mut);font-size:14px;margin:0 0 18px}
.status{display:flex;align-items:center;gap:11px;padding:13px 15px;border-radius:14px;margin-bottom:16px;
 border:1px solid var(--line);background:var(--card);font-weight:600;font-size:14.5px}
.status .ic{width:22px;height:22px;flex:0 0 auto}
.status.ok{border-color:rgba(63,185,80,.55);background:rgba(63,185,80,.13);color:#7ee08a}
.status.wait{border-color:rgba(210,153,34,.4);color:#e3b341}
.step{display:flex;gap:13px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:15px;margin:11px 0}
.num{flex:0 0 auto;width:28px;height:28px;border-radius:50%;background:#21262d;border:1px solid var(--line);
 display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;color:var(--acc)}
.step h3{margin:1px 0 5px;font-size:15px}.step p{margin:0;color:var(--mut);font-size:13.5px}.step b{color:var(--txt)}
.path{display:inline-block;background:#0b0e13;border:1px solid var(--line);border-radius:7px;padding:3px 8px;font-size:12.5px;margin-top:7px}
a.btn{display:block;text-align:center;text-decoration:none;font-weight:700;font-size:16px;padding:15px;border-radius:13px;margin:10px 0}
.dl{background:var(--acc2);color:#fff;border:1px solid var(--acc)}
.go{background:#21262d;color:var(--mut);border:1px solid var(--line);transition:.25s}
.go.ready{background:var(--acc);color:#06210d;border-color:var(--acc);box-shadow:0 6px 22px rgba(46,160,67,.4)}
.hint{color:var(--mut);font-size:12.5px;text-align:center;margin-top:14px}
code{background:#0b0e13;border:1px solid var(--line);padding:2px 6px;border-radius:6px;word-break:break-all;font-size:12.5px}
</style></head><body><div class=wrap>
<h1>&#128241; PhoneCam - Setup</h1>
<p class=sub>Install the certificate once, then open the camera. Takes about a minute.</p>

<div class="status wait" id=st>
 <svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><circle cx=12 cy=12 r=9/><path d="M12 7v5l3 2"/></svg>
 <span id=stt>Checking certificate&hellip;</span></div>

<a class="btn dl" href="/phonecam-ca.cer">&#11015; Download certificate</a>

<div class=step><div class=num>1</div><div><h3>Allow the download</h3>
 <p>In the pop-up tap <b>Allow</b>. Then it says &ldquo;Profile Downloaded&rdquo;.</p></div></div>
<div class=step><div class=num>2</div><div><h3>Install the profile</h3>
 <p>Open <b>Settings</b> &mdash; at the very top tap <b>Profile Downloaded</b> &rarr; <b>Install</b> (top-right) and enter your passcode.</p>
 <span class=path>Settings &rsaquo; Profile Downloaded &rsaquo; Install</span></div></div>
<div class=step><div class=num>3</div><div><h3>Trust the certificate <b>(required!)</b></h3>
 <p>Turn <b>ON</b> the switch next to &ldquo;PhoneCam Local CA&rdquo;.</p>
 <span class=path>Settings &rsaquo; General &rsaquo; About &rsaquo; Certificate Trust Settings</span></div></div>

<a class="btn go" id=go href="#">Open the camera &rarr;</a>
<p class=hint id=hint>The button turns green once the certificate is trusted.</p>
</div>
<script>
var HTTPS='__HTTPS__', KEY='__KEY__';
var host=location.hostname, base='https://'+host+':'+HTTPS+'/';
document.getElementById('go').href = base + (KEY ? ('?k='+KEY) : '');
var isiOS=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
if(!isiOS){ document.querySelector('.sub').innerHTML='Install the certificate, then open the camera. '+
 '(These steps are for iPhone; on other devices your browser will prompt to trust it.)'; }
var trusted=false;
function setTrusted(ok){
 if(ok===trusted) return; trusted=ok;
 var st=document.getElementById('st'), go=document.getElementById('go'), hint=document.getElementById('hint');
 if(ok){ st.className='status ok'; document.getElementById('stt').innerHTML='&#10003; Certificate trusted - you are ready!';
   go.className='btn go ready'; hint.textContent='Tap to open the camera, then press Start.'; }
 else { st.className='status wait'; document.getElementById('stt').innerHTML='Not trusted yet - finish steps 1-3 above.';
   go.className='btn go'; hint.textContent='The button turns green once the certificate is trusted.'; }
}
function check(){ fetch(base+'health',{mode:'no-cors',cache:'no-store'}).then(function(){setTrusted(true);}).catch(function(){setTrusted(false);}); }
check(); setInterval(check, 2500);
</script>
</body></html>"""


async def cert_index(request):
    key = request.app.get("relay", {}).get("key", "")
    html = (CERT_PAGE.replace("__HTTPS__", str(request.app["https_port"]))
                     .replace("__KEY__", key))
    return web.Response(text=html, content_type="text/html")


async def serve_ca(request):
    return web.FileResponse(CA_DER, headers={
        "Content-Type": "application/x-x509-ca-cert",
        "Content-Disposition": "attachment; filename=phonecam-ca.cer",
    })


async def viewer_index(request):
    return web.FileResponse(os.path.join(WEB_DIR, "viewer.html"), headers=_NOCACHE)


async def relay_handler(request):
    """MULTI-viewer signaling relay: 1 phone (sender) -> N viewers (app embedded browser,
    OBS browser source...). aiortc is NOT in the media path -> the browser decodes IN HARDWARE.
    The sender opens a separate PeerConnection per viewer; messages are addressed with 'to'/'from' vid."""
    if not _origin_ok(request):
        log.warning("Relay: cross-origin WS rejected (%s, origin=%s)", request.remote, request.headers.get("Origin"))
        return web.Response(status=403, text="forbidden")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    room = request.app["relay"]              # {"sender": ws|None, "viewers": {vid: ws}, "next_vid": int}
    hooks = request.app.get("hooks")
    role = None
    vid = None
    peer = request.remote
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type == WSMsgType.ERROR:
                    break
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            t = data.get("type")
            if t == "hello":
                r = data.get("role")
                rk = room.get("key")
                if r == "sender":
                    # anti-hijack: the sender (phone) may only log in with the correct key
                    if rk and data.get("key") != rk:
                        log.warning("Relay: sender REJECTED due to wrong/missing key (%s)", peer)
                        try:
                            await ws.send_json({"type": "denied"})
                        except Exception:
                            pass
                        break
                    role = "sender"
                    old = room.get("sender")
                    if old is not None and old is not ws:
                        # a new sender supersedes the old (e.g. the phone reconnecting after a blip).
                        # tell the displaced one so it stops cleanly instead of auto-reconnecting into
                        # a takeover ping-pong with the live sender.
                        try:
                            await old.send_json({"type": "superseded"})
                        except Exception:
                            pass
                        try:
                            await old.close()
                        except Exception:
                            pass
                    room["sender"] = ws
                    log.info("Relay: sender connected (%s)", peer)
                    if hooks and hooks.on_relay:
                        hooks.on_relay("sender", True)
                    for v in list(room["viewers"].keys()):     # viewers already waiting
                        try:
                            await ws.send_json({"type": "viewer-join", "id": v})
                        except Exception:
                            pass
                elif r == "viewer":
                    # local viewers (app preview + OBS on this same machine) may go without a key
                    if rk and data.get("key") != rk and not _is_local(peer):
                        log.warning("Relay: viewer REJECTED due to wrong/missing key (%s)", peer)
                        try:
                            await ws.send_json({"type": "denied"})
                        except Exception:
                            pass
                        break
                    role = "viewer"
                    vid = room["next_vid"]
                    room["next_vid"] += 1
                    room["viewers"][vid] = ws
                    log.info("Relay: viewer #%d connected (%s)", vid, peer)
                    if hooks and hooks.on_relay:
                        hooks.on_relay("viewer", True)
                    s = room.get("sender")
                    if s is not None:
                        try:
                            await s.send_json({"type": "viewer-join", "id": vid})
                        except Exception:
                            pass
            elif t in ("offer", "answer", "ice"):
                if role == "sender":
                    v = room["viewers"].get(data.get("to"))     # to a given viewer
                    if v is not None:
                        try:
                            await v.send_json(data)
                        except Exception:
                            pass
                elif role == "viewer":
                    s = room.get("sender")
                    if s is not None:
                        data["from"] = vid                       # tag which viewer it's from
                        try:
                            await s.send_json(data)
                        except Exception:
                            pass
            elif t == "meta":
                # phone -> app meta (e.g. battery status); not media, just for the device card
                if role == "sender" and hooks and getattr(hooks, "on_meta", None):
                    try:
                        hooks.on_meta(data)
                    except Exception:
                        pass
    finally:
        if role == "sender" and room.get("sender") is ws:
            room["sender"] = None
            for v in list(room["viewers"].values()):
                try:
                    await v.send_json({"type": "peer-gone"})
                except Exception:
                    pass
            if hooks and hooks.on_relay:
                hooks.on_relay("sender", False)
        elif role == "viewer" and vid is not None and room["viewers"].get(vid) is ws:
            del room["viewers"][vid]
            s = room.get("sender")
            if s is not None:
                try:
                    await s.send_json({"type": "viewer-leave", "id": vid})
                except Exception:
                    pass
            if hooks and hooks.on_relay:
                hooks.on_relay("viewer", False)
        log.info("Relay: %s closed (%s)", role, peer)
    return ws


class Hooks:
    """GUI/CLI callbacks. Any of them may be None. The calls come from another thread,
    so on the GUI side they must be forwarded in a thread-safe way (Qt signal)."""

    def __init__(self, on_state=None, on_frame=None, on_vcam=None, on_ready=None,
                 on_stats=None, on_relay=None, on_meta=None):
        self.on_state = on_state    # (peer: str, state: str)
        self.on_frame = on_frame    # (rgb: ndarray) or (None) at the end of the stream
        self.on_vcam = on_vcam      # (ok: bool, info: str)
        self.on_ready = on_ready    # (info: dict)
        self.on_stats = on_stats    # (stats: dict) — keys: res, fps, bitrate, loss
        self.on_relay = on_relay    # (role: str, connected: bool) — OBS path: sender/viewer
        self.on_meta = on_meta      # (data: dict) — phone meta, e.g. {"battery": {...}}


async def serve(args, ssl_ctx, ip, hooks=None, stop_event=None):
    relay_key = _relay_key()
    relay_room = {"sender": None, "viewers": {}, "next_vid": 1, "key": relay_key}   # shared between the HTTPS+HTTP apps

    app = web.Application()
    app["pcs"] = set()
    app["hooks"] = hooks
    app["relay"] = relay_room
    app["max_kbps"] = getattr(args, "max_kbps", MAX_KBPS)
    app["sink"] = VCamSink(args.width, args.height, args.fps, args.backend or None,
                           on_vcam=(hooks.on_vcam if hooks else None))
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/ice", ice_config)         # ICE servers (STUN/TURN)
    app.router.add_get("/manifest.webmanifest", manifest_handler)  # PWA
    app.router.add_get("/viewer", viewer_index)
    app.router.add_get("/relay", relay_handler)    # OBS path: browser-viewer signaling
    app.on_shutdown.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port, ssl_context=ssl_ctx).start()

    # HTTP app (ALWAYS running): cert installer + VIEWER page (OBS) + relay.
    # The viewer does NOT call getUserMedia -> no HTTPS needed -> the OBS browser source loads it fine.
    http_app = web.Application()
    http_app["https_port"] = args.port
    http_app["relay"] = relay_room
    http_app["hooks"] = hooks
    if os.path.exists(CA_DER):
        http_app.router.add_get("/", cert_index)
        http_app.router.add_get("/phonecam-ca.cer", serve_ca)
    http_app.router.add_get("/ice", ice_config)
    http_app.router.add_get("/manifest.webmanifest", manifest_handler)
    http_app.router.add_get("/viewer", viewer_index)
    http_app.router.add_get("/relay", relay_handler)
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    await web.TCPSite(http_runner, args.host, args.http_port).start()

    # internal aiortc viewer that feeds the virtual camera from the relay stream, so Discord's
    # "Unity Video Capture" works WITHOUT OBS (the phone's preview stays browser-decoded/smooth).
    bridge_task = asyncio.ensure_future(vcam_bridge(args.http_port, app["sink"], hooks, stop_event))

    # off the event loop: sync Zeroconf register_service blocks (multicast announce) for 100s of ms
    mdns = await asyncio.get_running_loop().run_in_executor(None, start_mdns, ip, args.port)

    _q = f"?k={relay_key}" if relay_key else ""
    info = {
        "ip": ip,
        "cam_url": f"https://{ip}:{args.port}/{_q}",
        "viewer_url": f"http://{ip}:{args.http_port}/viewer{_q}",
        "cert_url": (f"http://{ip}:{args.http_port}/" if os.path.exists(CA_DER) else None),
        "width": args.width, "height": args.height, "fps": args.fps,
        "backend": args.backend or "auto", "key": relay_key,
    }
    if hooks and hooks.on_ready:
        hooks.on_ready(info)
    else:
        print("\n" + "=" * 60)
        print("  PhoneCam server running")
        if info["cert_url"]:
            print(f"  [iPhone, once] Install certificate:    {info['cert_url']}")
        print(f"  [Camera]       Open on your phone:      {info['cam_url']}")
        print(f"  [OBS]          Browser source URL:      {info['viewer_url']}")
        print("=" * 60 + "\n")

    try:
        await (stop_event.wait() if stop_event is not None else asyncio.Event().wait())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except BaseException:
            pass
        if mdns:
            def _mdns_close(m):
                try:
                    zc, sinfo = m
                    zc.unregister_service(sinfo)
                    zc.close()
                except Exception:
                    pass
            try:
                await asyncio.get_running_loop().run_in_executor(None, _mdns_close, mdns)
            except Exception:
                pass
        await runner.cleanup()
        await http_runner.cleanup()


def build_ssl(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def make_args(port=8443, http_port=8080, backend="unitycapture",
              width=VCAM_W, height=VCAM_H, fps=VCAM_FPS,
              host="0.0.0.0", cert=None, key=None, max_kbps=MAX_KBPS):
    """Programmatic 'args' for the app (instead of an argparse Namespace)."""
    import types
    return types.SimpleNamespace(
        host=host, port=port, http_port=http_port,
        cert=cert or os.path.join(HERE, "cert.pem"),
        key=key or os.path.join(HERE, "key.pem"),
        backend=backend, width=width, height=height, fps=fps, max_kbps=max_kbps)


def main():
    ap = argparse.ArgumentParser(description="PhoneCam server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--cert", default=os.path.join(HERE, "cert.pem"))
    ap.add_argument("--key", default=os.path.join(HERE, "key.pem"))
    ap.add_argument("--backend", default="unitycapture",
                    help="pyvirtualcam backend: unitycapture | obs | (empty = auto)")
    ap.add_argument("--width", type=int, default=VCAM_W)
    ap.add_argument("--height", type=int, default=VCAM_H)
    ap.add_argument("--fps", type=int, default=VCAM_FPS)
    ap.add_argument("--max-kbps", type=int, default=MAX_KBPS, dest="max_kbps",
                    help="video b=AS ceiling in kbps (the phone may send up to this)")
    args = ap.parse_args()

    if not (os.path.exists(args.cert) and os.path.exists(args.key)):
        log.error("Certificate missing (%s / %s).", args.cert, args.key)
        log.error("Run first:  python server/make_cert.py")
        raise SystemExit(1)

    ip = lan_ip()
    try:
        asyncio.run(serve(args, build_ssl(args.cert, args.key), ip))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
