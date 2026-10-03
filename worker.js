// Web Worker: does the math off the main thread so the page stays responsive,
// and so app.js can terminate() a calculation that runs too long.
// Started as a module worker ({ type: "module" }): Pyodide v314 doesn't support classic workers.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

const ready = (async () => {
  const pyodide = await loadPyodide();
  await pyodide.loadPackage("sympy");
  // steps.py first: integrate.py uses its functions. Two literal fetch() calls so deploy.sh
  // can tag each URL for cache-busting.
  for (const response of [await fetch("steps.py?v=30a7a47"), await fetch("integrate.py?v=30a7a47")]) {
    if (!response.ok) throw new Error(`Couldn't fetch ${response.url} (${response.status})`);
    pyodide.runPython(await response.text());
  }
  return {
    run: pyodide.globals.get("run"),
    sampleView: pyodide.globals.get("sample_view"),
    pointAt: pyodide.globals.get("point_at"),
  };
})();

ready.then(
  () => postMessage({ type: "ready" }),
  (err) => postMessage({ type: "failed", error: String(err) }),
);

onmessage = async ({ data }) => {
  const { run, sampleView, pointAt } = await ready;
  if (data.type === "point") {
    let values;
    try {
      values = JSON.parse(pointAt(data.x));
    } catch (err) {
      console.error(err);
      values = { y: null, y2: null };
    }
    postMessage({ type: "point", id: data.id, x: data.x, ...values });
    return;
  }
  if (data.type === "sample") {
    let points;
    try {
      points = JSON.parse(sampleView(data.lo, data.hi));
    } catch (err) {
      console.error(err);
      points = { curve: [], area: [] };
    }
    postMessage({ type: "samples", id: data.id, ...points });
    return;
  }
  let result;
  try {
    result = JSON.parse(run(data.expr, data.lower, data.upper, data.mode, data.steps));
  } catch (err) {
    console.error(err);
    result = { ok: false, error: "Something went wrong while working that out." };
  }
  postMessage({ type: "result", id: data.id, ...result });
};
