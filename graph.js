// Interactive graph. Chart.js draws it; this file adds drag-to-pan, wheel/pinch zoom
// and reset, and asks the worker for fresh points whenever the view changes, so the
// curve never runs out however far you move.

const MIN_WIDTH = 1e-6; // zoom limits (width of the view in x or y)
const MAX_WIDTH = 1e6;
const RESAMPLE_DELAY_MS = 80;

const plotEl = document.getElementById("plot");
const canvas = document.getElementById("canvas");

let chart = null;
let homeView = null; // starting view, restored by Reset / double-click
let askForSamples = null; // (lo, hi) => request id; provided by app.js
let latestSampleId = null;
let fitYOnNextSamples = false;
let resampleTimer;
const pointers = new Map(); // pointerId -> {x, y}, for drag and pinch

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Neon glow on the curve (dataset 0). Chart.js has no built-in glow.
const glowPlugin = {
  id: "glow",
  beforeDatasetDraw(c, { index }) {
    if (index !== 0) return;
    c.ctx.save();
    c.ctx.shadowColor = cssVar("--accent");
    c.ctx.shadowBlur = 10;
  },
  afterDatasetDraw(c, { index }) {
    if (index === 0) c.ctx.restore();
  },
};

function axis([min, max]) {
  return {
    type: "linear",
    min,
    max,
    border: { display: false },
    // Brighter line at 0 so the x and y axes stand out from the grid.
    grid: { color: (ctx) => (ctx.tick?.value === 0 ? cssVar("--axis") : cssVar("--grid")) },
    // includeBounds: false -> only round tick values, not the view's raw edges (e.g. -0.785)
    ticks: { color: cssVar("--text-dim"), maxTicksLimit: 8, includeBounds: false, font: { family: cssVar("--mono"), size: 11 } },
  };
}

function showGraph(plot, requestSamples) {
  clearGraph();
  askForSamples = requestSamples;
  let xRange = [-10, 10];
  if (plot.a !== null) {
    const pad = 0.25 * (plot.b - plot.a || 1);
    xRange = [plot.a - pad, plot.b + pad];
  }
  homeView = { x: xRange, y: [-1, 1] };
  plotEl.hidden = false; // must be visible before Chart.js measures the canvas
  const line = { data: [], showLine: true, pointRadius: 0 };
  chart = new Chart(canvas, {
    type: "scatter",
    data: {
      datasets: [
        { ...line, borderWidth: 2, borderColor: cssVar("--accent") },
        { ...line, borderWidth: 0, fill: "origin", backgroundColor: cssVar("--accent-fill") },
      ],
    },
    options: {
      animation: false,
      maintainAspectRatio: false,
      events: [], // we handle the pointer ourselves
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: axis(homeView.x), y: axis(homeView.y) },
    },
    plugins: [glowPlugin],
  });
  fitYOnNextSamples = true;
  resample();
}

function clearGraph() {
  chart?.destroy();
  chart = null;
  latestSampleId = null;
  clearTimeout(resampleTimer);
  pointers.clear();
  plotEl.hidden = true;
}

function resample() {
  if (!chart) return;
  const { min, max } = chart.options.scales.x;
  const width = max - min;
  latestSampleId = askForSamples(min - width, max + width); // 3x the view: no empty edges while dragging
}

function scheduleResample() {
  clearTimeout(resampleTimer);
  resampleTimer = setTimeout(resample, RESAMPLE_DELAY_MS);
}

function applySamples({ id, curve, area }) {
  if (!chart || id !== latestSampleId) return; // stale reply
  chart.data.datasets[0].data = curve;
  chart.data.datasets[1].data = area;
  if (fitYOnNextSamples) {
    fitYOnNextSamples = false;
    homeView.y = fitY(curve);
    chart.options.scales.y.min = homeView.y[0];
    chart.options.scales.y.max = homeView.y[1];
  }
  chart.update("none");
}

// y-range for the visible points: 2nd-98th percentile (so a spike near an
// asymptote can't squash the plot), always including y = 0, plus 10% padding.
function fitY(curve) {
  const [xMin, xMax] = homeView.x;
  const ys = curve.filter(([xv, yv]) => yv !== null && xv >= xMin && xv <= xMax).map(([, yv]) => yv);
  if (!ys.length) return [-1, 1];
  ys.sort((p, q) => p - q);
  const pick = (fraction) => ys[Math.round(fraction * (ys.length - 1))];
  const lo = Math.min(0, pick(0.02));
  const hi = Math.max(0, pick(0.98));
  const pad = 0.1 * (hi - lo || 1);
  return [lo - pad, hi + pad];
}

function setView(xRange, yRange) {
  const { x, y } = chart.options.scales;
  [x.min, x.max] = xRange;
  [y.min, y.max] = yRange;
  chart.update("none");
  scheduleResample();
}

function currentView() {
  const { x, y } = chart.options.scales;
  return { x: [x.min, x.max], y: [y.min, y.max] };
}

// Zoom both axes by `factor` (<1 zooms in) around the pixel (px, py).
function zoomAt(px, py, factor) {
  const view = currentView();
  const cx = chart.scales.x.getValueForPixel(px);
  const cy = chart.scales.y.getValueForPixel(py);
  const scale = ([min, max], c) => [c - (c - min) * factor, c + (max - c) * factor];
  const xRange = scale(view.x, cx);
  const yRange = scale(view.y, cy);
  const ok = ([min, max]) => max - min >= MIN_WIDTH && max - min <= MAX_WIDTH;
  if (ok(xRange) && ok(yRange)) setView(xRange, yRange);
}

// Move the view so the graph follows a drag of (dxPx, dyPx) pixels.
function panBy(dxPx, dyPx) {
  const view = currentView();
  const { width, height } = chart.chartArea;
  const dx = (dxPx * (view.x[1] - view.x[0])) / width;
  const dy = (dyPx * (view.y[1] - view.y[0])) / height; // pixels grow downward, y grows upward
  setView([view.x[0] - dx, view.x[1] - dx], [view.y[0] + dy, view.y[1] + dy]);
}

function resetView() {
  if (chart) setView(homeView.x, homeView.y);
}

canvas.addEventListener("wheel", (event) => {
  if (!chart) return;
  event.preventDefault(); // zoom the graph instead of scrolling the page
  const delta = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY; // lines -> pixels (Firefox)
  zoomAt(event.offsetX, event.offsetY, Math.exp(delta * 0.0015));
}, { passive: false });

canvas.addEventListener("pointerdown", (event) => {
  if (!chart) return;
  canvas.setPointerCapture(event.pointerId);
  pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
});

canvas.addEventListener("pointermove", (event) => {
  if (!chart || !pointers.has(event.pointerId)) return;
  const before = [...pointers.values()];
  pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
  const after = [...pointers.values()];
  if (after.length === 1) {
    panBy(after[0].x - before[0].x, after[0].y - before[0].y);
  } else if (after.length === 2) {
    // Pinch: follow the midpoint of the two fingers, zoom by how their distance changed.
    const mid = (p) => ({ x: (p[0].x + p[1].x) / 2, y: (p[0].y + p[1].y) / 2 });
    const dist = (p) => Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
    const m0 = mid(before);
    const m1 = mid(after);
    panBy(m1.x - m0.x, m1.y - m0.y);
    zoomAt(m1.x, m1.y, dist(before) / dist(after));
  }
});

for (const type of ["pointerup", "pointercancel"]) {
  canvas.addEventListener(type, (event) => pointers.delete(event.pointerId));
}

canvas.addEventListener("dblclick", resetView);
document.getElementById("reset").addEventListener("click", resetView);
