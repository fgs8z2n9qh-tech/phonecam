"""
Automatic OBS Studio setup (obs-websocket v5) for the browser path:
  - creates/updates a Browser Source pointing at the VIEWER URL (http://<ip>:8080/viewer),
  - makes the scene active,
  - starts the Virtual Camera.

Uses an aiohttp WebSocket client -> NO new dependency.

The user has to enable this ONCE in OBS:
  OBS -> Tools -> WebSocket Server Settings -> "Enable WebSocket server".
  Port 4455. If there's a password (there is by default, auto-generated), "Show Connect Info" reveals it.
"""
import asyncio
import base64
import hashlib
import json

import aiohttp

SCENE = "PhoneCam"
SOURCE = "PhoneCam telefon"


def _auth_string(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


async def _req(ws, request_type, data=None, rid="r"):
    await ws.send_json({"op": 6, "d": {"requestType": request_type, "requestId": rid,
                                       "requestData": data or {}}})
    while True:
        msg = await ws.receive(timeout=6)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError("OBS WebSocket closed")
        m = json.loads(msg.data)
        if m.get("op") == 7 and m["d"].get("requestId") == rid:
            return m["d"]   # {requestType, requestStatus:{result,code,comment}, responseData}


async def _setup(viewer_url, password, host, port, width, height):
    url = f"ws://{host}:{port}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.ws_connect(url) as ws:
            msg = await ws.receive(timeout=6)
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError("OBS closed the connection during handshake "
                                   "(is the WebSocket server enabled, host/port correct?).")
            hello = json.loads(msg.data)
            ident = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
            auth = hello.get("d", {}).get("authentication")
            if auth:
                if not password:
                    raise PermissionError(
                        "The OBS WebSocket requires a password. Provide it (OBS -> Tools -> WebSocket "
                        "Server Settings -> Show Connect Info), or turn off 'Enable "
                        "Authentication' there.")
                ident["d"]["authentication"] = _auth_string(password, auth["salt"], auth["challenge"])
            await ws.send_json(ident)
            msg = await ws.receive(timeout=6)
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError("OBS authentication failed (wrong password?).")
            idd = json.loads(msg.data)
            if idd.get("op") != 2:
                raise RuntimeError("OBS authentication failed (wrong password?): %s" % idd)

            scenes = (await _req(ws, "GetSceneList"))["responseData"]["scenes"]
            if SCENE not in [x.get("sceneName") for x in scenes]:
                await _req(ws, "CreateScene", {"sceneName": SCENE})

            inputs = (await _req(ws, "GetInputList",
                                 {"inputKind": "browser_source"}))["responseData"]["inputs"]
            settings = {"url": viewer_url, "width": width, "height": height,
                        "shutdown": False, "restart_when_active": True, "webpage_control_level": 0}
            if SOURCE in [x.get("inputName") for x in inputs]:
                await _req(ws, "SetInputSettings",
                           {"inputName": SOURCE, "inputSettings": settings, "overlay": True})
            else:
                r = await _req(ws, "CreateInput", {
                    "sceneName": SCENE, "inputName": SOURCE, "inputKind": "browser_source",
                    "inputSettings": settings, "sceneItemEnabled": True})
                if r["requestStatus"]["result"] is False:
                    raise RuntimeError("Browser source error: " + str(r["requestStatus"].get("comment")))

            await _req(ws, "SetCurrentProgramScene", {"sceneName": SCENE})

            st = await _req(ws, "GetVirtualCamStatus")
            if not st["responseData"].get("outputActive"):
                await _req(ws, "StartVirtualCam")
            return {"ok": True, "scene": SCENE, "source": SOURCE}


def setup_obs(viewer_url, password=None, host="localhost", port=4455, width=1280, height=720):
    """Sync wrapper. Returns {'ok':True,...} or raises: PermissionError (password needed) /
    RuntimeError (OBS unreachable / WS server not enabled / other)."""
    try:
        return asyncio.run(_setup(viewer_url, password, host, port, width, height))
    except (aiohttp.ClientConnectorError, ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
        raise RuntimeError(
            "Can't reach OBS. Is OBS running? Is the WebSocket server enabled? "
            "(OBS -> Tools -> WebSocket Server Settings -> Enable WebSocket server, port 4455.)") from e
