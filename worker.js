// Web Worker: does the math off the main thread so the page stays responsive,
// and so app.js can terminate() a calculation that runs too long.
// Started as a module worker ({ type: "module" }): Pyodide v314 doesn't support classic workers.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

const ready = (async () => {
  const pyodide = await loadPyodide();
  await pyodide.loadPackage("sympy");
  const response = await fetch("integrate.py?v=8ea9d69");
  if (!response.ok) throw new Error(`Couldn't fetch integrate.py (${response.status})`);
  pyodide.runPython(await response.text());
  return { run: pyodide.globals.get("run"), sampleView: pyodide.globals.get("sample_view") };
})();

ready.then(
  () => postMessage({ type: "ready" }),
  (err) => postMessage({ type: "failed", error: String(err) }),
);

onmessage = async ({ data }) => {
  const { run, sampleView } = await ready;
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
    result = JSON.parse(run(data.expr, data.lower, data.upper));
  } catch (err) {
    console.error(err);
    result = { ok: false, error: "Something went wrong while integrating that." };
  }
  postMessage({ type: "result", id: data.id, ...result });
};
