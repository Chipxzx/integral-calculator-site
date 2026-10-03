"""Runs inside Pyodide (loaded by worker.js after steps.py). Safely parses the user's input and
integrates or differentiates it.

worker.js calls run(expr, lower, upper, mode, want_steps), sample_view(lo, hi) and point_at(x);
all return JSON strings. integral_steps_for(), derivative_steps(), add(), NoSteps, tex() and integrand_tex() come
from steps.py; tex() writes ln and arcsin/arctan the way a math class does.
"""
import json
import math
import re

from sympy import (
    Abs, E, Expr, Float, Function, Integer, Integral, Rational, Symbol, acos, asin, atan, cos,
    cosh, diff, exp, integrate, lambdify, log, nan, oo, pi, sin, sinh, sqrt, tan, tanh, zoo,
)
from sympy.parsing.sympy_parser import (
    convert_xor, implicit_multiplication, parse_expr, standard_transformations,
)

# Same allowlist as app.js. Checked again here in case the JS check is ever bypassed.
MAX_LEN = 200
CHARS = re.compile(r"^[0-9a-z+\-*/^(). ]+$")
x = Symbol("x")
x_real = Symbol("x", real=True)  # derivatives: so |x|' is sign(x), not a complex-number formula
NAMES = {
    "x": x, "e": E, "pi": pi,
    "sin": sin, "cos": cos, "tan": tan, "asin": asin, "acos": acos, "atan": atan,
    "sinh": sinh, "cosh": cosh, "tanh": tanh,
    "exp": exp, "log": log, "ln": log, "sqrt": sqrt, "abs": Abs,
}
# parse_expr turns "2" into Integer(2) etc., so only these are needed. No builtins.
GLOBALS = {"__builtins__": {}, "Integer": Integer, "Float": Float, "Rational": Rational, "Symbol": Symbol}
# implicit_multiplication: 10x, 2(x+1), (x+1)(x-1), 3x sin(x) multiply. It only inserts *
# between tokens that already passed the allowlist. convert_xor: ^ means power.
TRANSFORMS = standard_transformations + (implicit_multiplication, convert_xor)
CURVE_POINTS = 900  # graph.js asks for 3x the visible width, so ~300 points on screen
AREA_POINTS = 300
MAX_VIEW_WIDTH = 1e7

# The last function (and its derivative, in derivative mode), so the graph can ask for fresh
# points as you pan and zoom. dfn/dfn2 are their derivatives, to find maxima and minima.
VIEW = {"fn": None, "dfn": None, "fn2": None, "dfn2": None, "a": None, "b": None}
EMPTY_VIEW = dict(VIEW)
# Important points (intercepts, max/min, intersections): more than this many of one kind in
# view means the curve wiggles too fast for them to be useful, so that kind is skipped.
MAX_POINTS_PER_KIND = 40
# lambdify's "math" module has no sign(), which |x|' needs.
LAMBDIFY_MODULES = [{"sign": lambda v: (v > 0) - (v < 0)}, "math"]

NO_STEPS_NOTE = "No step-by-step method for this one; the answer above was found with SymPy's advanced algorithms."

# Special (non-elementary) functions SymPy may use in an answer, in plain words.
SPECIAL = {
    "erf": "the error function", "erfi": "the imaginary error function",
    "Si": "the sine integral", "Ci": "the cosine integral",
    "Shi": "the hyperbolic sine integral", "Chi": "the hyperbolic cosine integral",
    "Ei": "the exponential integral", "expint": "the generalized exponential integral",
    "li": "the logarithmic integral",
    "fresnels": "a Fresnel integral", "fresnelc": "a Fresnel integral",
    "gamma": "the gamma function", "uppergamma": "the upper incomplete gamma function",
    "lowergamma": "the lower incomplete gamma function",
    "LambertW": "the Lambert W function", "polylog": "the polylogarithm",
}

# Answer check: compare values at these points (fixed, so results are repeatable).
TEST_POINTS = (0.37, 1.13, 2.71, -0.83, 1.9, 3.3, -2.2)
MIN_USABLE_POINTS = 3
CHECK_UNKNOWN = {"status": "unknown", "text": "Couldn't double-check this one."}


class UserError(Exception):
    """A problem to show the user as-is."""


def parse(text, allow_x, error, variable=x):
    text = text.strip()
    if not text or len(text) > MAX_LEN or not CHARS.match(text):
        raise UserError(error)
    for word in re.findall(r"[a-z]+", text):
        if word not in NAMES or (word == "x" and not allow_x):
            raise UserError(error)
    try:
        expr = parse_expr(text, local_dict=dict(NAMES, x=variable), global_dict=dict(GLOBALS), transformations=TRANSFORMS)
    except Exception:
        raise UserError(error)
    if not isinstance(expr, Expr):  # e.g. "sin" on its own
        raise UserError(error)
    return expr


def parse_limit(text):
    value = parse(text, allow_x=False, error="Couldn't read the limits.")
    if not (value.is_extended_real and value.is_finite):
        raise UserError("Limits must be finite real numbers.")
    return value


def safe_value(fn, xv):
    """fn(xv) as a float, or None where it's undefined (log(-1), 1/0, complex values...)."""
    try:
        yv = float(fn(xv))
    except Exception:
        return None
    return yv if math.isfinite(yv) else None


def sample(fn, lo, hi, count):
    """count evenly spaced [x, y] pairs; y is None where f is undefined (Chart.js leaves a gap)."""
    return [[xv, safe_value(fn, xv)] for xv in (lo + (hi - lo) * i / count for i in range(count + 1))]


def remember(f, a, b, variable=x, derivative=None):
    """Store f (and f') for sample_view(). Returns the plot info for the page, or None if f can't be plotted."""
    try:
        fn = lambdify(variable, f, LAMBDIFY_MODULES)
        dfn = lambdify(variable, diff(f, variable), LAMBDIFY_MODULES)
        fn2 = dfn2 = None
        if derivative is not None:
            fn2 = lambdify(variable, derivative, LAMBDIFY_MODULES)
            dfn2 = lambdify(variable, diff(derivative, variable), LAMBDIFY_MODULES)
    except Exception:
        return None
    VIEW.update(fn=fn, dfn=dfn, fn2=fn2, dfn2=dfn2, a=a, b=b)
    return {"a": a, "b": b, "derivative": derivative is not None}


def special_notes(answer):
    """One note per special function the answer uses, e.g. erf."""
    names = sorted({fn.func.__name__ for fn in answer.atoms(Function)} & SPECIAL.keys())
    return [f"Uses {name} ({SPECIAL[name]}), a special function with no elementary formula." for name in names]


def close(got, want, tolerance):
    return abs(got - want) <= tolerance * max(1, abs(want))


def agree(got, want, variable=x):
    """Compare two expressions at TEST_POINTS (numbers, not simplify(): fast).
    True if equal wherever both are defined, False if not, None if too few usable points."""
    usable = 0
    for v in TEST_POINTS:
        try:
            w = complex(want.subs(variable, v).evalf())
            g = complex(got.subs(variable, v).evalf())
        except Exception:  # undefined at this point
            continue
        if not all(map(math.isfinite, (w.real, w.imag, g.real, g.imag))):
            continue
        if not close(g, w, 1e-8):
            return False
        usable += 1
    return None if usable < MIN_USABLE_POINTS else True


def check_antiderivative(f, antiderivative):
    """Differentiate the answer and compare it with f."""
    same = agree(diff(antiderivative, x), f)
    if same is None:
        return CHECK_UNKNOWN
    if not same:
        return {"status": "failed", "text": "✗ Check failed: differentiating the answer doesn't give back f(x)."}
    return {"status": "ok", "text": "✓ Checked: d/dx of the answer gives back f(x)."}


def check_definite(f, a, b, value):
    """Compare the exact answer with plain numerical integration (mpmath quadrature)."""
    try:
        numeric = complex(Integral(f, (x, a, b)).evalf())
    except Exception:
        return CHECK_UNKNOWN
    if not (math.isfinite(numeric.real) and math.isfinite(numeric.imag)):
        return CHECK_UNKNOWN
    if not close(numeric, value, 1e-6):
        return {"status": "failed", "text": "✗ Check failed: the exact answer doesn't match numerical integration."}
    return {"status": "ok", "text": "✓ Checked: matches numerical integration."}


def add_integral_steps(result, f, answer, limits=None):
    """Attach steps to result, or a note if there's no step-by-step method.
    limits = (a, b, exact) for a definite integral."""
    try:
        antiderivative, steps = integral_steps_for(f, x)
        if agree(diff(antiderivative, x), f) is not True:  # never show steps we can't verify
            raise NoSteps
        if limits is None:
            add(steps, 0, "Add the constant of integration.",
                rf"\int {integrand_tex(f)}\, dx = {tex(antiderivative)} + C")
            if tex(antiderivative) != tex(answer):
                add(steps, 0, "This is the same as the answer above, just written differently "
                              "(antiderivatives can also differ by a constant).")
        else:
            a, b, exact = limits
            Fb, Fa = antiderivative.subs(x, b), antiderivative.subs(x, a)
            try:
                fits = close(complex((Fb - Fa).evalf()), complex(exact.evalf()), 1e-9)
            except Exception:  # F undefined at a limit
                fits = False
            if fits:
                add(steps, 0, "Evaluate between the limits: F(b) − F(a).",
                    rf"\Big[{tex(antiderivative)}\Big]_{{{tex(a)}}}^{{{tex(b)}}} = "
                    rf"{tex(Fb)} - \left({tex(Fa)}\right) = {tex(exact)}")
            else:
                add(steps, 0, "Apply the limits to get the answer above "
                              "(here SymPy needed a limit at an endpoint, so F(b) − F(a) alone isn't enough).")
    except NoSteps:
        result["notes"].append(NO_STEPS_NOTE)
        return
    result["steps"] = steps


def average_rate(f, lower_text, upper_text):
    """(a, b, f(a), f(b), rate) for the average rate of change (f(b) − f(a)) / (b − a)."""
    a, b = parse_limit(lower_text), parse_limit(upper_text)
    if a == b:
        raise UserError("From and To must be different.")
    fa, fb = f.subs(x_real, a), f.subs(x_real, b)
    for value in (fa, fb):
        try:
            number = complex(value.evalf())
        except Exception:
            raise UserError("f isn't defined at From or To.")
        if not (math.isfinite(number.real) and abs(number.imag) <= 1e-12 * max(1, abs(number.real))):
            raise UserError("f isn't defined at From or To.")
    return a, b, fa, fb, (fb - fa) / (b - a)


def solve_derivative(expr_text, lower_text, upper_text, want_steps):
    f = parse(expr_text, allow_x=True, error="Couldn't read that expression.", variable=x_real)
    has_lower, has_upper = bool(lower_text.strip()), bool(upper_text.strip())
    if has_lower != has_upper:
        raise UserError("Fill in both limits.")
    rate = average_rate(f, lower_text, upper_text) if has_lower else None
    answer = diff(f, x_real)
    result = {
        "inputLatex": rf"\frac{{d}}{{dx}}\left[{tex(f)}\right]",
        "resultLatex": tex(answer),
        "notes": special_notes(answer),
        "plot": remember(f, None, None, variable=x_real, derivative=answer),
    }
    rate_step = None
    if rate:
        a, b, fa, fb, value = rate
        result["rate"] = {"latex": tex(value), "decimal": float(value.evalf()), "from": tex(a), "to": tex(b)}
        if result["plot"]:  # start the graph around [a, b] and draw the secant line
            result["plot"].update(a=min(float(a), float(b)), b=max(float(a), float(b)),
                                  secant=[[float(a), float(fa.evalf())], [float(b), float(fb.evalf())]])
        rate_step = ("Average rate of change from a to b: (f(b) − f(a)) / (b − a), the slope of the secant line.",
                     rf"\frac{{f\left({tex(b)}\right) - f\left({tex(a)}\right)}}{{{tex(b)} - {tex(a)}}} = "
                     rf"\frac{{{tex(fb)} - \left({tex(fa)}\right)}}{{{tex(b)} - {tex(a)}}} = {tex(value)}")
    if not want_steps:
        return result
    try:
        ours, steps = derivative_steps(f, x_real)
        same = agree(ours, answer, x_real)
        if same is False:  # a bug in our step engine: don't show wrong steps
            raise NoSteps
        if tex(ours) != tex(answer):
            add(steps, 0, "Simplify.", rf"\frac{{d}}{{dx}}\left[{tex(f)}\right] = {tex(answer)}")
        if rate_step:
            add(steps, 0, *rate_step)
    except NoSteps:
        result["notes"].append("Couldn't break this one into steps; the answer above is from SymPy.")
        return result
    result["steps"] = steps
    result["check"] = CHECK_UNKNOWN if same is None else {
        "status": "ok", "text": "✓ Checked: the steps agree with SymPy's derivative."}
    return result


def solve(expr_text, lower_text, upper_text, mode, want_steps):
    VIEW.update(EMPTY_VIEW)
    if mode == "differentiate":
        return solve_derivative(expr_text, lower_text, upper_text, want_steps)
    f = parse(expr_text, allow_x=True, error="Couldn't read that expression.")

    if not lower_text.strip() and not upper_text.strip():
        antiderivative = integrate(f, x)
        result = {"inputLatex": tex(Integral(f, x)), "plot": remember(f, None, None)}
        if antiderivative.has(Integral):  # SymPy gave up: no elementary antiderivative
            result["notes"] = ["No elementary antiderivative: this can't be written with standard functions."]
            return result
        result["resultLatex"] = tex(antiderivative) + " + C"
        result["notes"] = special_notes(antiderivative)
        result["check"] = check_antiderivative(f, antiderivative)
        if want_steps:
            add_integral_steps(result, f, antiderivative)
        return result

    if not lower_text.strip() or not upper_text.strip():
        raise UserError("Fill in both limits.")
    a, b = parse_limit(lower_text), parse_limit(upper_text)

    exact = integrate(f, (x, a, b))
    if exact.has(oo, -oo, zoo, nan):
        raise UserError("This integral diverges.")
    closed_form = not exact.has(Integral)
    # No closed form: integrate numerically. (SymPy's NonElementaryIntegral.evalf() doesn't do that,
    # but a plain Integral's evalf() runs mpmath quadrature.)
    value = complex((exact if closed_form else Integral(f, (x, a, b))).evalf())
    if not math.isfinite(value.real) or abs(value.imag) > 1e-12 * max(1, abs(value.real)):
        raise UserError("The result isn't a finite real number.")

    lo, hi = sorted((float(a), float(b)))
    result = {
        "inputLatex": tex(Integral(f, (x, a, b))),
        "decimal": value.real,
        "plot": remember(f, lo, hi),
    }
    if not closed_form:  # show the numerical value only
        result["notes"] = ["No closed form, so this is a numerical value."]
        return result
    result["resultLatex"] = tex(exact)
    result["notes"] = special_notes(exact)
    result["check"] = check_definite(f, a, b, value)
    if want_steps:
        add_integral_steps(result, f, exact, limits=(a, b, exact))
    return result


def bisect(g, lo, hi, g_lo):
    """Narrow [lo, hi], where g changes sign, down to the crossing (60 halvings: full precision)."""
    for _ in range(60):
        mid = (lo + hi) / 2
        g_mid = safe_value(g, mid)
        if g_mid is None:
            return None
        if g_mid == 0:
            return mid
        if (g_mid < 0) == (g_lo < 0):
            lo, g_lo = mid, g_mid
        else:
            hi = mid
    return (lo + hi) / 2


def crossings(g, xs, gs):
    """(x, direction) where g crosses zero between the sample points xs (gs = g at xs).
    direction is +1 going up, -1 going down. Touching zero without crossing doesn't count."""
    found = []
    for i in range(len(xs) - 1):
        g0, g1 = gs[i], gs[i + 1]
        if g0 is None or g1 is None or g1 == 0:  # a sample exactly at 0 is handled as g0 next time
            continue
        if g0 == 0:
            before = gs[i - 1] if i > 0 else None
            if before is None or before == 0 or (before < 0) == (g1 < 0):
                continue
            found.append((xs[i], 1 if g1 > 0 else -1))
        elif (g0 < 0) != (g1 < 0):
            root = bisect(g, xs[i], xs[i + 1], g0)
            g_root = None if root is None else safe_value(g, root)
            # A jump across an asymptote also changes sign (1/x at 0): only keep real zeros.
            if g_root is None or abs(g_root) > 1e-6 * max(1.0, abs(g0), abs(g1)):
                continue
            found.append((root, 1 if g1 > 0 else -1))
        if len(found) > MAX_POINTS_PER_KIND:
            return []
    return found


def curve_points(fn, dfn, samples, curve):
    """Important points of one curve in the sampled range: max/min, x-intercepts, y-intercept."""
    xs = [p[0] for p in samples]
    points = []
    for xv, direction in crossings(dfn, xs, [safe_value(dfn, v) for v in xs]):
        yv = safe_value(fn, xv)
        if yv is not None:  # f' goes - to + at a min, + to - at a max
            points.append({"curve": curve, "kind": "min" if direction > 0 else "max", "x": xv, "y": yv})
    for xv, _ in crossings(fn, xs, [p[1] for p in samples]):
        points.append({"curve": curve, "kind": "x-intercept", "x": xv, "y": 0.0})
    y0 = safe_value(fn, 0.0)
    if xs[0] <= 0 <= xs[-1] and y0 is not None:
        points.append({"curve": curve, "kind": "y-intercept", "x": 0.0, "y": y0})
    # One point per spot: a max/min beats an intercept beats the y-intercept (x² at 0 is "min").
    step = xs[1] - xs[0]
    kept = []
    for p in points:
        if all(abs(p["x"] - k["x"]) > step / 2 for k in kept):
            kept.append(p)
    return kept


def intersections(fn, fn2, samples, samples2):
    """Where f and f' cross (derivative tab, when both curves are shown)."""
    xs = [p[0] for p in samples]
    gaps = [None if (p[1] is None or q[1] is None) else p[1] - q[1] for p, q in zip(samples, samples2)]
    found = crossings(lambda v: fn(v) - fn2(v), xs, gaps)
    return [{"curve": "both", "kind": "intersection", "x": xv, "y": safe_value(fn, xv)}
            for xv, _ in found if safe_value(fn, xv) is not None]


def sample_view(lo, hi):
    """Points for the graph between lo and hi: the curve, the shaded part of [a, b] in view,
    f' (curve2) in derivative mode, and the important points of each curve."""
    lo, hi = float(lo), float(hi)
    fn, fn2, a, b = VIEW["fn"], VIEW["fn2"], VIEW["a"], VIEW["b"]
    if fn is None or not (math.isfinite(lo) and math.isfinite(hi) and 0 < hi - lo <= MAX_VIEW_WIDTH):
        return json.dumps({"curve": [], "area": [], "curve2": [], "points": []})
    area = []
    if a is not None and max(lo, a) < min(hi, b):
        area = sample(fn, max(lo, a), min(hi, b), AREA_POINTS)
    curve = sample(fn, lo, hi, CURVE_POINTS)
    points = curve_points(fn, VIEW["dfn"], curve, "f")
    curve2 = []
    if fn2 is not None:
        curve2 = sample(fn2, lo, hi, CURVE_POINTS)
        points += curve_points(fn2, VIEW["dfn2"], curve2, "df") + intersections(fn, fn2, curve, curve2)
    return json.dumps({"curve": curve, "area": area, "curve2": curve2, "points": points})


def point_at(xv):
    """Exact f(x) and f'(x) (None where undefined) for the graph's click-to-read point."""
    xv = float(xv)

    def value(fn):
        return None if fn is None or not math.isfinite(xv) else safe_value(fn, xv)

    return json.dumps({"y": value(VIEW["fn"]), "y2": value(VIEW["fn2"])})


def run(expr_text, lower_text, upper_text, mode="integrate", want_steps=True):
    try:
        result = {"ok": True, **solve(expr_text, lower_text, upper_text, mode, want_steps)}
    except UserError as err:
        result = {"ok": False, "error": str(err)}
    except Exception:
        result = {"ok": False, "error": "Something went wrong while working that out."}
    return json.dumps(result)
