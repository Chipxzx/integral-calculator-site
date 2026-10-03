"""Runs inside Pyodide (loaded by worker.js). Safely parses the user's input and integrates it.

worker.js calls run(expr, lower, upper) and sample_view(lo, hi); both return JSON strings.
"""
import json
import math
import re

from sympy import (
    Abs, E, Expr, Float, Function, Integer, Integral, Rational, Symbol, acos, asin, atan, cos,
    cosh, diff, exp, integrate, latex, lambdify, log, nan, oo, pi, sin, sinh, sqrt, tan, tanh, zoo,
)
from sympy.parsing.sympy_parser import (
    convert_xor, implicit_multiplication, parse_expr, standard_transformations,
)

# Same allowlist as app.js. Checked again here in case the JS check is ever bypassed.
MAX_LEN = 200
CHARS = re.compile(r"^[0-9a-z+\-*/^(). ]+$")
x = Symbol("x")
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

# The last integrated function, so the graph can ask for fresh points as you pan and zoom.
VIEW = {"fn": None, "a": None, "b": None}

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


def parse(text, allow_x, error):
    text = text.strip()
    if not text or len(text) > MAX_LEN or not CHARS.match(text):
        raise UserError(error)
    for word in re.findall(r"[a-z]+", text):
        if word not in NAMES or (word == "x" and not allow_x):
            raise UserError(error)
    try:
        expr = parse_expr(text, local_dict=dict(NAMES), global_dict=dict(GLOBALS), transformations=TRANSFORMS)
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


def sample(fn, lo, hi, count):
    """count evenly spaced [x, y] pairs; y is None where f is undefined (Chart.js leaves a gap)."""
    points = []
    for i in range(count + 1):
        xv = lo + (hi - lo) * i / count
        try:
            yv = float(fn(xv))
        except Exception:  # log(-1), 1/0, complex values...
            yv = None
        if yv is not None and not math.isfinite(yv):
            yv = None
        points.append([xv, yv])
    return points


def remember(f, a, b):
    """Store f for sample_view(). Returns the plot info for the page, or None if f can't be plotted."""
    try:
        fn = lambdify(x, f, "math")
    except Exception:
        return None
    VIEW.update(fn=fn, a=a, b=b)
    return {"a": a, "b": b}


def special_notes(answer):
    """One note per special function the answer uses, e.g. erf."""
    names = sorted({fn.func.__name__ for fn in answer.atoms(Function)} & SPECIAL.keys())
    return [f"Uses {name} ({SPECIAL[name]}), a special function with no elementary formula." for name in names]


def close(got, want, tolerance):
    return abs(got - want) <= tolerance * max(1, abs(want))


def check_antiderivative(f, antiderivative):
    """Differentiate the answer and compare it with f at TEST_POINTS (numbers, not simplify(): fast)."""
    derivative = diff(antiderivative, x)
    usable = 0
    for v in TEST_POINTS:
        try:
            want = complex(f.subs(x, v).evalf())
            got = complex(derivative.subs(x, v).evalf())
        except Exception:  # undefined at this point
            continue
        if not all(map(math.isfinite, (want.real, want.imag, got.real, got.imag))):
            continue
        if not close(got, want, 1e-8):
            return {"status": "failed", "text": "✗ Check failed: differentiating the answer doesn't give back f(x)."}
        usable += 1
    if usable < MIN_USABLE_POINTS:
        return CHECK_UNKNOWN
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


def solve(expr_text, lower_text, upper_text):
    VIEW.update(fn=None, a=None, b=None)
    f = parse(expr_text, allow_x=True, error="Couldn't read that expression.")

    if not lower_text.strip() and not upper_text.strip():
        antiderivative = integrate(f, x)
        result = {"inputLatex": latex(Integral(f, x)), "plot": remember(f, None, None)}
        if antiderivative.has(Integral):  # SymPy gave up: no elementary antiderivative
            result["notes"] = ["No elementary antiderivative: this can't be written with standard functions."]
            return result
        result["resultLatex"] = latex(antiderivative) + " + C"
        result["notes"] = special_notes(antiderivative)
        result["check"] = check_antiderivative(f, antiderivative)
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
        "inputLatex": latex(Integral(f, (x, a, b))),
        "decimal": value.real,
        "plot": remember(f, lo, hi),
    }
    if not closed_form:  # show the numerical value only
        result["notes"] = ["No closed form, so this is a numerical value."]
        return result
    result["resultLatex"] = latex(exact)
    result["notes"] = special_notes(exact)
    result["check"] = check_definite(f, a, b, value)
    return result


def sample_view(lo, hi):
    """Points for the graph between lo and hi: the curve, plus the shaded part of [a, b] in view."""
    lo, hi = float(lo), float(hi)
    fn, a, b = VIEW["fn"], VIEW["a"], VIEW["b"]
    if fn is None or not (math.isfinite(lo) and math.isfinite(hi) and 0 < hi - lo <= MAX_VIEW_WIDTH):
        return json.dumps({"curve": [], "area": []})
    area = []
    if a is not None and max(lo, a) < min(hi, b):
        area = sample(fn, max(lo, a), min(hi, b), AREA_POINTS)
    return json.dumps({"curve": sample(fn, lo, hi, CURVE_POINTS), "area": area})


def run(expr_text, lower_text, upper_text):
    try:
        result = {"ok": True, **solve(expr_text, lower_text, upper_text)}
    except UserError as err:
        result = {"ok": False, "error": str(err)}
    except Exception:
        result = {"ok": False, "error": "Something went wrong while integrating that."}
    return json.dumps(result)
