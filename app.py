import os
import time
import math
import asyncio
import threading
from typing import List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

# -----------------------------
# GPIO / Geiger configuration
# -----------------------------
PIN = int(os.getenv("GEIGER_PIN", "18"))  # GPIO17 = pin físico 11

# Si tu salida es push-pull a 3.3V tras divisor: PULL_UP=False y evento "activated".
# Si tu salida es open-collector activa a GND:
#   pon GEIGER_PULL_UP=1 para usar pull_up True.
PULL_UP = os.getenv("GEIGER_PULL_UP", "0") == "1"

# Modo mock para desarrollo sin hardware:
MOCK = os.getenv("GEIGER_MOCK", "0") == "1"
MOCK_RATE = float(os.getenv("GEIGER_MOCK_RATE", "5.0"))  # pulsos/seg aprox

# Límite de deltas enviados al cliente (para no inflar memoria)
MAX_DELTAS = int(os.getenv("GEIGER_MAX_DELTAS", "2000"))
MAX_SERIES = int(os.getenv("GEIGER_MAX_SERIES", "3600"))  # hasta 1h por defecto

# -----------------------------
# Estado global
# -----------------------------
class GeigerState:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.t0 = time.time()
            self.total = 0
            self.last_ts: Optional[float] = None
            self.timestamps: List[float] = []  # tiempos absolutos de pulsos
            self.deltas: List[float] = []      # Δt en segundos
            self.per_second: List[int] = []    # conteos integrados por segundo
            self._current_second_count = 0

    def on_pulse(self, ts: float):
        with self.lock:
            self.total += 1
            if self.last_ts is not None:
                dt = ts - self.last_ts
                if dt >= 0:
                    self.deltas.append(dt)
                    if len(self.deltas) > MAX_DELTAS:
                        self.deltas = self.deltas[-MAX_DELTAS:]
            self.last_ts = ts
            self.timestamps.append(ts)
            # recorta timestamps si crece mucho
            if len(self.timestamps) > MAX_DELTAS * 2:
                self.timestamps = self.timestamps[-MAX_DELTAS * 2:]

            self._current_second_count += 1

    def tick_second(self):
        """Llamar cada 1s para cerrar el bin actual."""
        with self.lock:
            self.per_second.append(self._current_second_count)
            self._current_second_count = 0
            if len(self.per_second) > MAX_SERIES:
                self.per_second = self.per_second[-MAX_SERIES:]

    def snapshot(self):
        with self.lock:
            now = time.time()
            elapsed = max(0.0, now - self.t0)
            seconds = int(elapsed)

            series = list(self.per_second)
            # Ojo: el bin actual aún no cerrado no se incluye (es deseado)
            n_bins = len(series)

            # Estimación de tasa media (actividad efectiva observada)
            # Usamos total/elapsed; error Poisson ~ sqrt(N)/T
            if elapsed > 0:
                rate = self.total / elapsed
                err = math.sqrt(self.total) / elapsed if self.total > 0 else 0.0
            else:
                rate, err = 0.0, 0.0

            # Media acumulada por segundo para dibujar en cliente
            # (podemos enviar solo rate global y el cliente calcula running mean,
            #  pero enviamos también un vector opcional para comodidad)
            running_mean = []
            s = 0
            for i, c in enumerate(series, start=1):
                s += c
                running_mean.append(s / i)

            # Edad del último pulso
            last_age = (now - self.last_ts) if self.last_ts else None

            return {
                "total": self.total,
                "elapsed": elapsed,
                "seconds": seconds,
                "last_age": last_age,
                "per_second": series,
                "running_mean": running_mean,
                "rate_bq": rate,   # "actividad efectiva observada"
                "rate_err": err,
                "deltas": list(self.deltas),
            }


state = GeigerState()

# -----------------------------
# WebSocket manager
# -----------------------------
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

    async def broadcast(self, message: dict):
        async with self.lock:
            clients = list(self.clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    self.clients.discard(ws)

manager = WSManager()

# Guardamos el loop principal para poder notificar desde callback GPIO (hilo)
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None

def schedule_broadcast(msg: dict):
    global MAIN_LOOP
    if MAIN_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(msg), MAIN_LOOP)
    except Exception:
        pass

# -----------------------------
# GPIO setup (real or mock)
# -----------------------------
def start_geiger_reader():
    if MOCK:
        def mock_thread():
            # Proceso Poisson simple
            import random
            while True:
                # espera exponencial con lambda=MOCK_RATE
                lam = max(0.0001, MOCK_RATE)
                dt = random.expovariate(lam)
                time.sleep(dt)
                ts = time.time()
                state.on_pulse(ts)
                schedule_broadcast({"type": "pulse", "ts": ts})
        t = threading.Thread(target=mock_thread, daemon=True)
        t.start()
        return

    try:
        from gpiozero import DigitalInputDevice
        from gpiozero.pins.lgpio import LGPIOFactory

        factory = LGPIOFactory()
        dev = DigitalInputDevice(PIN, pull_up=PULL_UP, pin_factory=factory)

        def _pulse():
            ts = time.time()
            state.on_pulse(ts)
            schedule_broadcast({"type": "pulse", "ts": ts})

        # Si pull_up=False: pulso activo HIGH -> when_activated
        # Si pull_up=True: típico open-collector -> pulso activo LOW.
        # En ese caso, usamos when_deactivated como "pulso" (transición a LOW -> activado sería False)
        if not PULL_UP:
            dev.when_activated = _pulse
        else:
            # Con pull-up, la línea queda HIGH; un pulso suele bajarla.
            # when_deactivated se dispara al pasar a LOW.
            dev.when_deactivated = _pulse

    except Exception as e:
        # Si falla el acceso GPIO, caemos a mock para que la app no muera
        print("WARNING: No se pudo iniciar GPIO real. Activando mock.")
        print(f"Detalle: {e}")
        os.environ["GEIGER_MOCK"] = "1"
        def mock_thread():
            import random
            while True:
                lam = max(0.0001, MOCK_RATE)
                dt = random.expovariate(lam)
                time.sleep(dt)
                ts = time.time()
                state.on_pulse(ts)
                schedule_broadcast({"type": "pulse", "ts": ts})
        t = threading.Thread(target=mock_thread, daemon=True)
        t.start()

# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="Geiger Live")

HTML = r"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Geiger Live</title>

  <!-- Chart.js desde CDN -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

  <style>
    :root{
      --bg0: #070b14;
      --bg1: #0b1224;
      --glow: #39ffcc;
      --accent: #ff3df2;
      --accent2: #7c5cff;
      --warn: #ffb020;
      --text: #e8f1ff;
      --muted: #9db0d1;
      --panel: rgba(255,255,255,0.04);
      --panel-border: rgba(255,255,255,0.08);
      --ok: #4dff7a;
    }

    *{box-sizing:border-box}
    body{
      margin:0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, "Helvetica Neue", Arial, "Noto Sans", sans-serif;
      color: var(--text);
      background:
        radial-gradient(1200px 600px at 10% 10%, rgba(124,92,255,0.12), transparent 60%),
        radial-gradient(1000px 800px at 90% 20%, rgba(255,61,242,0.10), transparent 55%),
        radial-gradient(900px 700px at 50% 90%, rgba(57,255,204,0.10), transparent 60%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
      min-height:100vh;
      display:flex;
      justify-content:center;
      padding: 32px 16px 64px;
    }

    .col{
      width:min(960px, 100%);
      display:flex;
      flex-direction:column;
      gap: 18px;
    }

    .hero{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap: 12px;
      padding: 18px 22px;
      border: 1px solid var(--panel-border);
      background: linear-gradient(135deg, rgba(57,255,204,0.08), rgba(255,61,242,0.06), rgba(124,92,255,0.08));
      border-radius: 16px;
      position:relative;
      overflow:hidden;
    }

    .hero::after{
      content:"";
      position:absolute;
      inset:-40%;
      background: conic-gradient(from 0deg, transparent, rgba(57,255,204,0.08), transparent, rgba(255,61,242,0.08), transparent);
      animation: spin 18s linear infinite;
      filter: blur(18px);
      pointer-events:none;
    }

    @keyframes spin { to { transform: rotate(360deg); } }

    .title{
      display:flex;
      align-items:center;
      gap: 14px;
      z-index:1;
    }

    .rad{
      font-size: 34px;
      line-height:1;
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(255,176,32,0.12);
      border: 1px solid rgba(255,176,32,0.35);
      text-shadow: 0 0 14px rgba(255,176,32,0.45);
    }

    h1{
      font-size: clamp(22px, 3.2vw, 30px);
      margin:0;
      letter-spacing: 0.6px;
    }

    .subtitle{
      margin: 2px 0 0;
      color: var(--muted);
      font-size: 13px;
    }

    .controls{
      display:flex;
      align-items:center;
      gap: 10px;
      z-index:1;
      flex-wrap:wrap;
    }

    .btn{
      border: 1px solid var(--panel-border);
      background: var(--panel);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 12px;
      cursor:pointer;
      font-weight: 600;
      letter-spacing: 0.2px;
      transition: 120ms ease;
    }
    .btn:hover{ transform: translateY(-1px); border-color: rgba(255,255,255,0.18); }
    .btn:active{ transform: translateY(0px) scale(0.99); }

    .btn-reset{
      background:
        linear-gradient(135deg, rgba(255,61,242,0.18), rgba(124,92,255,0.18));
      border-color: rgba(255,61,242,0.35);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.03);
    }

    .toggle{
      display:flex;
      align-items:center;
      gap: 8px;
      padding: 8px 10px;
      border-radius: 10px;
      border: 1px solid var(--panel-border);
      background: var(--panel);
      font-size: 12px;
      color: var(--muted);
    }
    .toggle input{ transform: translateY(1px); }

    .panel{
      border: 1px solid var(--panel-border);
      background: var(--panel);
      border-radius: 16px;
      padding: 16px 18px 14px;
    }

    .panel h2{
      margin: 0 0 10px;
      font-size: 16px;
      color: #f7fbff;
      letter-spacing: 0.4px;
    }

    .metrics{
      display:grid;
      grid-template-columns: repeat(3, minmax(0,1fr));
      gap: 10px;
    }
    @media (max-width: 720px){
      .metrics{ grid-template-columns: 1fr; }
      .hero{ flex-direction:column; align-items:flex-start; }
    }

    .metric{
      padding: 14px 14px 12px;
      border-radius: 14px;
      border: 1px solid var(--panel-border);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.00));
      position:relative;
      overflow:hidden;
    }

    .metric .label{
      font-size: 11px;
      color: var(--muted);
      letter-spacing: 0.3px;
    }
    .metric .value{
      font-size: clamp(22px, 3.4vw, 30px);
      font-weight: 800;
      margin-top: 2px;
      display:flex;
      align-items:baseline;
      gap: 8px;
    }
    .unit{
      font-size: 11px;
      color: var(--muted);
      font-weight: 600;
    }

    .blink-dot{
      width: 10px; height: 10px;
      border-radius: 999px;
      background: rgba(57,255,204,0.25);
      border: 1px solid rgba(57,255,204,0.3);
      box-shadow: 0 0 0 0 rgba(57,255,204,0.0);
      transition: 80ms ease;
      margin-left: 6px;
    }
    .blink-dot.active{
      background: var(--glow);
      box-shadow:
        0 0 10px rgba(57,255,204,0.9),
        0 0 24px rgba(57,255,204,0.55);
      transform: scale(1.25);
    }

    .small{
      font-size: 11px;
      color: var(--muted);
      margin-top: 6px;
    }

    .chart-wrap{
      height: 260px;
    }
    canvas{
      width: 100% !important;
      height: 100% !important;
    }

    .caption{
      display:flex;
      align-items:center;
      gap: 10px;
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(57,255,204,0.22);
      background: rgba(57,255,204,0.06);
      font-size: 12px;
      color: #eafff9;
    }
    .caption b{
      color: white;
      font-weight: 800;
    }

    .foot{
      color: var(--muted);
      font-size: 11px;
      padding: 6px 4px 0;
      text-align:center;
    }
  </style>
</head>

<body>
  <div class="col">

    <div class="hero">
      <div class="title">
        <div class="rad">☢</div>
        <div>
          <h1>Geiger Live Lab</h1>
          <div class="subtitle">Conteo en vivo · Serie temporal · Histograma de Δt</div>
        </div>
      </div>

      <div class="controls">
        <button class="btn btn-reset" id="resetBtn">RESET GENERAL</button>
        <label class="toggle">
          <input type="checkbox" id="soundToggle" />
          Sonido “pi!”
        </label>
      </div>
    </div>

    <div class="panel">
      <h2>1) Modo Conteo</h2>
      <div class="metrics">
        <div class="metric">
          <div class="label">Conteo total desde el último reset</div>
          <div class="value">
            <span id="totalCount">0</span>
            <span class="blink-dot" id="blinkDot"></span>
          </div>
          <div class="small">Parpadea en cada detección</div>
        </div>

        <div class="metric">
          <div class="label">Segundos desde el último reset</div>
          <div class="value">
            <span id="elapsedSec">0</span>
            <span class="unit">s</span>
          </div>
          <div class="small">Cronómetro interno de la sesión</div>
        </div>

        <div class="metric">
          <div class="label">Tiempo desde el último pulso</div>
          <div class="value">
            <span id="lastAge">—</span>
            <span class="unit">s</span>
          </div>
          <div class="small">Útil para ver si “hay vida” en el flujo</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>2) Plot temporal (integrado por segundo)</h2>
      <div class="chart-wrap">
        <canvas id="timeChart"></canvas>
      </div>
      <div class="caption" id="activityCaption">
        Actividad efectiva observada: <b>0.00</b> ± <b>0.00</b> Bq
      </div>
      <div class="small">
        Se dibuja el conteo por segundo y la media acumulada. Estimación simple con error Poisson.
      </div>
    </div>

    <div class="panel">
      <h2>3) Histograma de Δt</h2>
      <div class="chart-wrap">
        <canvas id="dtChart"></canvas>
      </div>
      <div class="small">
        Δt entre detecciones sucesivas. Para un proceso de Poisson ideal debería aproximar una exponencial.
      </div>
    </div>

    <div class="foot">
      Consejo: si tu salida es open-collector activa a GND, ejecuta con GEIGER_PULL_UP=1.
    </div>

  </div>

<script>
  // ---------------------------
  // Audio "pi!"
  // ---------------------------
  let audioCtx = null;

  function beep(){
    const enabled = document.getElementById("soundToggle").checked;
    if(!enabled) return;

    if(!audioCtx){
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }

    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();

    o.type = "sine";
    o.frequency.value = 1200;

    g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.08, audioCtx.currentTime + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.08);

    o.connect(g).connect(audioCtx.destination);
    o.start();
    o.stop(audioCtx.currentTime + 0.09);
  }

  // ---------------------------
  // Blink
  // ---------------------------
  function blink(){
    const dot = document.getElementById("blinkDot");
    dot.classList.add("active");
    setTimeout(()=>dot.classList.remove("active"), 90);
  }

  // ---------------------------
  // Charts
  // ---------------------------
  const timeCtx = document.getElementById("timeChart");
  const dtCtx = document.getElementById("dtChart");

  const timeChart = new Chart(timeCtx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Cuentas por segundo", data: [], tension: 0.25 },
        { label: "Media acumulada", data: [], tension: 0.25 }
      ]
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { ticks: { maxTicksLimit: 8 } },
        y: { beginAtZero: true }
      },
      plugins: {
        legend: { display: true }
      }
    }
  });

  const dtChart = new Chart(dtCtx, {
    type: "bar",
    data: {
      labels: [],
      datasets: [
        { label: "Frecuencia", data: [] }
      ]
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { ticks: { maxTicksLimit: 10 } },
        y: { beginAtZero: true }
      },
      plugins: {
        legend: { display: true }
      }
    }
  });

  function updateTimeChart(perSecond, runningMean){
    const n = perSecond.length;
    const labels = Array.from({length:n}, (_, i)=> i+1);

    timeChart.data.labels = labels;
    timeChart.data.datasets[0].data = perSecond;
    timeChart.data.datasets[1].data = runningMean;
    timeChart.update();
  }

  function updateDtHistogram(deltas){
    if(!deltas || deltas.length < 2){
      dtChart.data.labels = [];
      dtChart.data.datasets[0].data = [];
      dtChart.update();
      return;
    }

    // Bins adaptativos simples
    const maxDt = Math.max(...deltas);
    const minDt = Math.min(...deltas);

    // Limitar rango visual para que no se estropee con outliers enormes
    const cap = Math.max(1.5, Math.min(10.0, maxDt));
    const filtered = deltas.filter(d => d >= 0 && d <= cap);

    const bins = 24;
    const lo = 0.0;
    const hi = cap;
    const w = (hi - lo) / bins;

    const counts = new Array(bins).fill(0);
    for(const d of filtered){
      let idx = Math.floor((d - lo) / w);
      if(idx < 0) idx = 0;
      if(idx >= bins) idx = bins - 1;
      counts[idx] += 1;
    }

    const labels = counts.map((_, i)=>{
      const a = (lo + i*w).toFixed(2);
      const b = (lo + (i+1)*w).toFixed(2);
      return `${a}-${b}s`;
    });

    dtChart.data.labels = labels;
    dtChart.data.datasets[0].data = counts;
    dtChart.update();
  }

  function updateCaption(rate, err){
    const cap = document.getElementById("activityCaption");
    const r = (rate ?? 0).toFixed(2);
    const e = (err ?? 0).toFixed(2);
    cap.innerHTML = `Actividad efectiva observada: <b>${r}</b> ± <b>${e}</b> Bq`;
  }

  // ---------------------------
  // Reset
  // ---------------------------
  document.getElementById("resetBtn").addEventListener("click", async ()=>{
    await fetch("/api/reset", { method: "POST" });
  });

  // ---------------------------
  // WebSocket
  // ---------------------------
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${wsProto}://${location.host}/ws`);

  ws.onmessage = (ev)=>{
    const msg = JSON.parse(ev.data);

    if(msg.type === "pulse"){
      // feedback inmediato
      blink();
      beep();
      return;
    }

    if(msg.type === "snapshot"){
      document.getElementById("totalCount").textContent = msg.total ?? 0;
      document.getElementById("elapsedSec").textContent = Math.floor(msg.elapsed ?? 0);

      if(msg.last_age == null){
        document.getElementById("lastAge").textContent = "—";
      }else{
        document.getElementById("lastAge").textContent = (msg.last_age).toFixed(2);
      }

      updateTimeChart(msg.per_second ?? [], msg.running_mean ?? []);
      updateDtHistogram(msg.deltas ?? []);
      updateCaption(msg.rate_bq ?? 0, msg.rate_err ?? 0);
    }

    if(msg.type === "reset_ack"){
      // reseteo visual inmediato
      document.getElementById("totalCount").textContent = 0;
      document.getElementById("elapsedSec").textContent = 0;
      document.getElementById("lastAge").textContent = "—";
      updateTimeChart([], []);
      updateDtHistogram([]);
      updateCaption(0,0);
    }
  };

  ws.onopen = ()=>console.log("WS conectado");
  ws.onclose = ()=>console.log("WS cerrado");
</script>

</body>
</html>
"""

@app.get("/")
def index():
    return HTMLResponse(HTML)

@app.post("/api/reset")
def api_reset():
    state.reset()
    # Notificamos a clientes por WS desde el loop principal si existe
    schedule_broadcast({"type": "reset_ack"})
    return JSONResponse({"ok": True})

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # enviamos snapshot inicial
        await ws.send_json({"type": "snapshot", **state.snapshot()})
        while True:
            # mantenemos el WS vivo; no esperamos mensajes del cliente
            await asyncio.sleep(3600)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)

# -----------------------------
# Tarea asíncrona cada segundo
# -----------------------------
async def second_loop():
    while True:
        await asyncio.sleep(1.0)
        state.tick_second()
        snap = state.snapshot()
        await manager.broadcast({"type": "snapshot", **snap})

@app.on_event("startup")
async def on_startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

    # iniciar lector de pulsos en hilo/callback
    start_geiger_reader()

    # iniciar loop de snapshots por segundo
    asyncio.create_task(second_loop())

