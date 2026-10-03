// Main thread: validates input, talks to the math worker, and shows results.
// Security: user input only reaches SymPy after the allowlist check below, and
// anything the user typed is only ever put on the page with textContent.

const MAX_LEN = 200;
const CHARS = /^[0-9a-z+\-*/^(). ]+$/;
const WORDS = new Set([
  "x", "e", "pi",
  "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
  "exp", "log", "ln", "sqrt", "abs",
]);
const TIMEOUT_MS = 10000;

const $ = (id) => document.getElementById(id);
const form = $("form");
const exprInput = $("expr");
const lowerInput = $("lower");
const upperInput = $("upper");
const button = $("go");
const statusEl = $("status");
const errorEl = $("error");
const resultEl = $("result");
const formulaEl = $("formula");
const decimalEl = $("decimal");
const notesEl = $("notes");
const checkEl = $("check");
const copyButton = $("copy");
const chips = document.querySelectorAll(".chip");
const keys = document.querySelectorAll(".key");
const touchScreen = matchMedia("(hover: none) and (pointer: coarse)"); // same rule that shows the keypad

let worker;
let pendingId = null;
let nextId = 0;
let timer;
let renderedLatex = ""; // what Copy LaTeX copies
let copyTimer;
let lastInput = exprInput; // where keypad keys type

// Returns true if text passes the allowlist (same rules as integrate.py).
function isAllowed(text, allowX) {
  if (!text || text.length > MAX_LEN || !CHARS.test(text)) return false;
  const words = text.match(/[a-z]+/g) || [];
  return words.every((w) => WORDS.has(w) && (allowX || w !== "x"));
}

function validate(expr, lower, upper) {
  if (!isAllowed(expr, true)) return "Couldn't read that expression.";
  if (!lower && !upper) return null;
  if (!lower || !upper) return "Fill in both limits.";
  if (!isAllowed(lower, false) || !isAllowed(upper, false)) return "Couldn't read the limits.";
  return null;
}

function setBusy(busy, message) {
  button.disabled = busy;
  for (const chip of chips) chip.disabled = busy;
  statusEl.textContent = message;
}

function showError(message) {
  errorEl.textContent = message;
  errorEl.hidden = false;
}

function clearOutput() {
  errorEl.hidden = true;
  resultEl.hidden = true;
  clearGraph();
}

// 2 -> "2.0", 5.869604401089358 -> "5.869604401"
function formatDecimal(n) {
  const s = String(Number(n.toPrecision(10)));
  return /[.e]/.test(s) ? s : s + ".0";
}

// graph.js calls this when the view changes; returns the request id (or null).
function requestSamples(lo, hi) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  const id = ++nextId;
  worker.postMessage({ type: "sample", id, lo, hi });
  return id;
}

function showResult(data) {
  if (!data.ok) return showError(data.error);
  const hasExact = data.resultLatex !== undefined;
  const hasDecimal = data.decimal !== undefined;
  if (hasExact) renderedLatex = `${data.inputLatex} = ${data.resultLatex}`;
  else if (hasDecimal) renderedLatex = `${data.inputLatex} \\approx ${formatDecimal(data.decimal)}`; // no closed form
  else renderedLatex = data.inputLatex; // no elementary antiderivative
  katex.render(renderedLatex, formulaEl, { displayMode: true, throwOnError: false });

  decimalEl.hidden = !(hasExact && hasDecimal); // without an exact answer, the formula already shows ≈
  if (hasDecimal) decimalEl.textContent = `≈ ${formatDecimal(data.decimal)}`;

  // Notes and the check text come from integrate.py (our own fixed sentences), shown as text only.
  const notes = (data.notes || []).map((text) => Object.assign(document.createElement("li"), { textContent: text }));
  notesEl.replaceChildren(...notes);
  notesEl.hidden = !notes.length;
  checkEl.hidden = !data.check;
  if (data.check) {
    checkEl.textContent = data.check.text;
    checkEl.dataset.status = data.check.status;
  }

  copyButton.textContent = "Copy LaTeX";
  resultEl.hidden = false;
  if (data.plot) showGraph(data.plot, requestSamples);

  // On phones: close the keyboard and bring the answer into view.
  if (touchScreen.matches) {
    document.activeElement?.blur();
    resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function startWorker() {
  worker = new Worker("worker.js?v=8ea9d69", { type: "module" });
  setBusy(true, "Loading math engine…");
  // Fires if worker.js itself fails to load (e.g. the CDN is unreachable).
  worker.onerror = () => setBusy(true, "Couldn't load the math engine. Check your connection and reload the page.");
  worker.onmessage = ({ data }) => {
    if (data.type === "ready") {
      setBusy(false, "");
    } else if (data.type === "failed") {
      console.error(data.error);
      setBusy(true, "Couldn't load the math engine. Check your connection and reload the page.");
    } else if (data.type === "result" && data.id === pendingId) {
      clearTimeout(timer);
      pendingId = null;
      setBusy(false, "");
      showResult(data);
    } else if (data.type === "samples") {
      applySamples(data);
    }
  };
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (pendingId !== null || button.disabled) return; // busy, or the engine isn't loaded yet
  clearOutput();

  const expr = exprInput.value.trim();
  const lower = lowerInput.value.trim();
  const upper = upperInput.value.trim();
  const problem = validate(expr, lower, upper);
  if (problem) return showError(problem);

  pendingId = ++nextId;
  worker.postMessage({ type: "integrate", id: pendingId, expr, lower, upper });
  setBusy(true, "Integrating…");
  timer = setTimeout(() => {
    worker.terminate();
    pendingId = null;
    showError("That took too long, so I stopped it.");
    startWorker();
  }, TIMEOUT_MS);
});

// Example chips fill the form and submit it, so they go through the same validation as typing.
for (const chip of chips) {
  chip.addEventListener("click", () => {
    exprInput.value = chip.dataset.expr;
    lowerInput.value = chip.dataset.lower ?? "";
    upperInput.value = chip.dataset.upper ?? "";
    form.requestSubmit();
  });
}

// Keypad: insert the key's text at the cursor of the last focused input. Like the chips,
// this only changes the input's text, which is validated on submit as usual.
for (const input of [exprInput, lowerInput, upperInput]) {
  input.addEventListener("focus", () => (lastInput = input));
}

function insertKey(key) {
  const inputs = [exprInput, lowerInput, upperInput];
  const input = inputs.includes(document.activeElement) ? document.activeElement : lastInput;
  input.setRangeText(key.dataset.insert, input.selectionStart, input.selectionEnd, "end");
  input.focus();
}

for (const key of keys) {
  // preventDefault stops the tap/click from moving focus to the key,
  // so the input keeps focus and the phone keyboard stays open.
  key.addEventListener("mousedown", (event) => event.preventDefault());
  key.addEventListener("touchend", (event) => {
    event.preventDefault(); // also cancels the click that would follow
    insertKey(key);
  });
  key.addEventListener("click", () => insertKey(key)); // mouse and keyboard
}

copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(renderedLatex);
    copyButton.textContent = "Copied ✓";
  } catch {
    copyButton.textContent = "Copy failed";
  }
  clearTimeout(copyTimer);
  copyTimer = setTimeout(() => (copyButton.textContent = "Copy LaTeX"), 1500);
});

startWorker();
