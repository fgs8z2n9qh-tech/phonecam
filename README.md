<div align="center">

# 📱➜🎥 PhoneCam

**Turn your iPhone into a wireless webcam for your PC — over Wi‑Fi, nothing to install on the phone.**

Use your phone's (much better) camera in Discord, Zoom, OBS, Teams, or any app that takes a webcam.
Just open a link in Safari — no phone app, no cable, no account.

</div>

---

## Why

Your phone has a far better camera than most laptop or USB webcams. PhoneCam streams it to your PC
over your home Wi‑Fi and shows the live picture in a small desktop app. Optionally, it feeds that
picture into a **virtual camera** so any app — Discord, Zoom, Teams, OBS — can use it.

- **No app on the phone.** The phone just opens a web page in Safari (uses the browser's camera + WebRTC).
- **No OBS required, no C++ build.** A tiny Python signaling relay does the plumbing; the picture is
  decoded by real browsers for smooth, high‑fps video.
- **Runs entirely on your LAN.** No cloud, no account, no data leaves your network.
- **Portable build available** — a single folder that ships its own Python. Double‑click and go.

---

## Quick start (portable — recommended)

1. Download from the [latest release](../../releases/latest) — either flavour:
   - **`PhoneCam-SingleFile.exe`** — one file, nothing to unzip. The first run unpacks itself to
     `%LOCALAPPDATA%\PhoneCam` (progress bar, ~20 s); every later run starts instantly.
   - **`PhoneCam-Portable.zip`** — the same app as a folder; unzip it anywhere.
2. Double‑click **`PhoneCam-SingleFile.exe`** (or **`PhoneCam.exe`** inside the unzipped folder).
   The window opens with a **QR code** and two web addresses.
   - If Windows shows a blue *"Windows protected your PC"* box: **More info → Run anyway** (it's unsigned, not harmful).
3. **Allow the firewall** (first time only). When Windows pops up the Defender Firewall dialog,
   tick **Private networks → Allow access**.
4. **On the iPhone — install the certificate** (first time only; Safari only allows the camera over
   a *trusted* HTTPS connection):
   - In Safari, open the **Certificate** address shown in the window (looks like `http://192.168.x.x:8080/`).
   - Tap to download the profile → **Allow**.
   - **Settings** → *"Profile Downloaded"* → **Install**.
   - **Settings → General → About → Certificate Trust Settings** → turn **ON** "PhoneCam Local CA".
     *(This switch is required on iOS.)*
5. **On the iPhone — start the camera.** In Safari, **scan the QR code** (or open the `https://…:8443/`
   address) → press **Start** → allow the camera. The live picture appears in the PhoneCam window. ✅

> **Same Wi‑Fi network** on phone and PC is required (STUN/TURN‑free, direct LAN connection).

---

## Use it in Discord / Zoom / Teams (optional)

Watching the picture in the PhoneCam window needs nothing extra. To feed it into a call you need a
**virtual camera**:

- **Built‑in (no OBS):** run `install_unitycapture.ps1` **once as Administrator** to register the
  [Unity Capture](https://github.com/schellingb/UnityCapture) virtual camera. PhoneCam then pipes the
  phone's video straight into it — pick **"Unity Video Capture"** as the camera in Discord/Zoom.
- **OBS route:** install OBS Studio, enable *Tools → WebSocket Server Settings* once, then click the
  **OBS** button in the PhoneCam window — it creates the browser source and starts the OBS Virtual
  Camera automatically. Pick **"OBS Virtual Camera"** in your call.

Only **video** is sent — keep using your normal microphone.

---

## Phone controls

The phone page is a full‑screen preview with a pull‑up control layer:

- **Zoom** — vertical slider on the right edge, or pinch. Zooming auto‑picks the best lens (0.5× / 1× / 2×), like the iOS camera app.
- **Lens buttons** — 0.5× / 1× / 2× / Front.
- **Gear** — Bitrate, FPS, Resolution, Flash, Mirror, 180° rotate, framing grid & level, and image
  adjustments (brightness / contrast / saturation).
- 60 fps works on the main camera at 720p on most iPhones; higher bitrate (10–15 Mbps) = sharper on good Wi‑Fi.
- Auto‑reconnect: if you switch apps and come back, it re‑opens the camera and re‑connects.

---

## How it works

```
📱 iPhone Safari (getUserMedia + WebRTC)
        │  Wi‑Fi / LAN
        ▼
🖥️  PhoneCam (Python aiohttp) — signaling relay only, multi‑viewer
        ├─►  App preview  (embedded browser, hardware decode → smooth fps)
        └─►  Virtual‑camera bridge  →  Unity Capture / OBS  →  🎮 Discord / Zoom / Teams
```

The Python server only relays WebRTC signaling — the video itself is decoded by real browser engines
(the in‑app preview and, if used, OBS), which is why the picture stays smooth even at high fps. A small
internal bridge additionally decodes one copy of the stream to feed the DirectShow virtual camera, so
Discord sees exactly what your phone sends.

**Why the certificate?** Browsers only hand out the camera over a secure (HTTPS) context. On a LAN
there's no public CA, so PhoneCam generates its own **local CA + leaf certificate** at first run
(`server/make_cert.py`) and you trust it once on the phone. The CA private key never leaves your PC
and is **never committed to this repo** (see [Security](#security)).

---

## Run from source

Requires **Python 3.12** on Windows 10/11.

```powershell
# 1. Create the environment, install deps, generate the cert, and launch the GUI:
powershell -ExecutionPolicy Bypass -File run_app.ps1

# (advanced) headless server, no GUI:
powershell -ExecutionPolicy Bypass -File run.ps1
```

Key files:

| Path | Role |
|---|---|
| `app.py` | Desktop GUI (PySide6): live preview, QR code, virtual‑camera picker, one‑click setup |
| `server/server.py` | aiohttp HTTPS + WebSocket signaling relay + virtual‑camera bridge |
| `server/make_cert.py` | Generates the local CA + server certificate (per machine, never committed) |
| `web/index.html` | Phone capture page (getUserMedia + WebRTC offerer) |
| `web/viewer.html` | Browser viewer page (used by OBS Browser Source) |
| `Launcher.cs` | Tiny C# `.exe` launcher used by the portable build |
| `install_unitycapture.ps1` | Registers the Unity Capture virtual camera (Admin) |
| `allow_firewall.ps1` | Opens ports 8080/8443 on the Private profile (Admin) |
| `requirements.txt` | aiohttp, aiortc, av, numpy, pyvirtualcam, cryptography, PySide6, qrcode, pillow |

Ports: **8080** (HTTP — certificate download) and **8443** (HTTPS — camera page).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Phone says *"server cannot be found"* | Firewall (step 3), or phone/PC are on different Wi‑Fi networks. |
| Safari won't turn on the camera | The certificate isn't trusted yet — the *Certificate Trust Settings* switch (step 4). |
| App shows a white / blank window | An old copy is still running. Close all PhoneCam windows and relaunch (a single‑instance guard prevents port clashes). |
| Discord shows a green/garbled image | Make sure a virtual camera is installed (Unity Capture or OBS) and selected; restart Discord after installing it. |
| App won't open at all | Run `Start-PhoneCam (debug).bat` (portable) to see the error. |

---

## Security

- The **local CA private key and certificates are never committed.** They're generated per machine at
  first run and are covered by `.gitignore` (`*.key`, `*.pem`, `*.cer`, …). The portable ZIP is built
  with a guard that **aborts if any key/cert material is present**.
- The relay is protected by an auto‑minted token embedded in the QR/URL, and cross‑origin WebSocket
  upgrades are rejected — so a stray page on your network can't hijack the camera slot.
- Everything stays on your LAN. No STUN/TURN, no cloud relay, no telemetry.

---

## License

[MIT](LICENSE). Includes on‑demand use of [Unity Capture](https://github.com/schellingb/UnityCapture)
(© Bernhard Schelling, MIT) — downloaded by the install script, not redistributed here.
