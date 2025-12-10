import os
import asyncio
from typing import Optional, Set

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from geiger import GeigerConfig, GeigerState, GeigerReader


load_dotenv()

cfg = GeigerConfig.from_env()
state = GeigerState(cfg)
reader = GeigerReader(cfg)

app = FastAPI(title="Geiger Web (RPi.GPIO backend)")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class WSManager:
    def __init__(self):
        self.clients: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, msg: dict):
        async with self.lock:
            clients = list(self.clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    self.clients.discard(ws)

manager = WSManager()
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def schedule_broadcast(msg: dict):
    global MAIN_LOOP
    if MAIN_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(msg), MAIN_LOOP)
    except Exception:
        pass


def on_pulse(ts: float):
    state.on_pulse(ts)
    schedule_broadcast({"type": "pulse", "ts": ts})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "pin": cfg.pin,
        "verbose": cfg.verbose,
        "mock": cfg.mock,
        "pid": os.getpid(),
    })


@app.post("/api/reset")
def api_reset():
    state.reset()
    schedule_broadcast({"type": "reset_ack"})
    if cfg.verbose:
        print("[APP] RESET via API")
    return JSONResponse({"ok": True})


@app.get("/api/snapshot")
def api_snapshot():
    return JSONResponse(state.snapshot())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "snapshot", **state.snapshot()})
        while True:
            await asyncio.sleep(3600)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


async def second_loop():
    while True:
        await asyncio.sleep(1.0)
        state.tick_second()
        await manager.broadcast({"type": "snapshot", **state.snapshot()})


@app.on_event("startup")
async def on_startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

    if cfg.verbose:
        print(f"[APP] startup pid={os.getpid()} GPIO{cfg.pin} mock={cfg.mock}")

    reader.set_callback(on_pulse)
    reader.start()

    asyncio.create_task(second_loop())


@app.on_event("shutdown")
async def on_shutdown():
    reader.stop()
    if cfg.verbose:
        print("[APP] shutdown")

