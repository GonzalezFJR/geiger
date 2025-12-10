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
// Blink UI
// ---------------------------
function blink(){
  const dot = document.getElementById("blinkDot");
  dot.classList.add("active");
  setTimeout(()=>dot.classList.remove("active"), 90);
}

// ---------------------------
// Charts
// ---------------------------
const timeChart = new Chart(document.getElementById("timeChart"), {
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
    }
  }
});

const dtChart = new Chart(document.getElementById("dtChart"), {
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
    }
  }
});

function updateCaption(rate, err){
  const cap = document.getElementById("activityCaption");
  const r = (rate ?? 0).toFixed(2);
  const e = (err ?? 0).toFixed(2);
  cap.innerHTML = `Actividad efectiva observada: <b>${r}</b> ± <b>${e}</b> Bq`;
}

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

  const maxDt = Math.max(...deltas);
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

// ---------------------------
// Debug UI
// ---------------------------
const debugToggle = document.getElementById("debugToggle");
const debugBox = document.getElementById("debugBox");
debugToggle.addEventListener("change", ()=>{
  debugBox.classList.toggle("hidden", !debugToggle.checked);
});

function updateDebug(obj){
  if(!debugToggle.checked) return;
  const pre = document.getElementById("debugPre");
  pre.textContent = JSON.stringify(obj, null, 2);
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
    blink();
    beep();
    return;
  }

  if(msg.type === "reset_ack"){
    document.getElementById("totalCount").textContent = 0;
    document.getElementById("elapsedSec").textContent = 0;
    document.getElementById("lastAge").textContent = "—";
    updateTimeChart([], []);
    updateDtHistogram([]);
    updateCaption(0, 0);
    updateDebug(msg);
    return;
  }

  if(msg.type === "snapshot"){
    document.getElementById("totalCount").textContent = msg.total ?? 0;
    document.getElementById("elapsedSec").textContent = Math.floor(msg.elapsed ?? 0);

    if(msg.last_age == null){
      document.getElementById("lastAge").textContent = "—";
    }else{
      document.getElementById("lastAge").textContent =
        (msg.last_age).toFixed(2);
    }

    updateTimeChart(msg.per_second ?? [], msg.running_mean ?? []);
    updateDtHistogram(msg.deltas ?? []);
    updateCaption(msg.rate_bq ?? 0, msg.rate_err ?? 0);

    updateDebug(msg);
  }
};

ws.onopen  = ()=>console.log("WS conectado");
ws.onclose = ()=>console.log("WS cerrado");

