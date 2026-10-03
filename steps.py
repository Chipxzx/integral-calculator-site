"""Step-by-step explanations. Loaded by worker.js before integrate.py, which calls
integral_steps_for() and derivative_steps().

Every step is {"depth": int, "text": str, "latex": str | None}: `text` is one of our own
fixed sentences (shown as plain text), `latex` comes from SymPy (rendered by KaTeX), and
`depth` indents sub-steps.
"""
from sympy import (
    Abs, Add, Mul, S, Symbol, acos, asin, atan, cos, cosh, diff, exp, fraction, latex, log, sec,
    sign, sin, sinh, sqrt, tan, tanh,
)
from sympy.integrals import manualintegrate as mi

MAX_STEPS = 80  # beyond this, steps stop being readable


def tex(expr):
    """LaTeX the way a math class writes it: ln (not log), arcsin/arctan (not asin/atan).
    integrate.py uses this too, so answers and steps look the same."""
    return latex(expr, ln_notation=True, inv_trig_style="full")


def integrand_tex(expr):
    """An integrand in brackets when it's a sum, so ∫ (3x² + 2x) dx doesn't read as 3x² + ∫ 2x dx."""
    return rf"\left({tex(expr)}\right)" if expr.is_Add else tex(expr)


class NoSteps(Exception):
    """No step-by-step explanation available for this input."""


def add(steps, depth, text, math=None):
    if len(steps) >= MAX_STEPS:
        raise NoSteps
    steps.append({"depth": depth, "text": text, "latex": math})


# ---------------------------------------------------------------- integrals

# Sentences for SymPy's "atomic" rules (no sub-steps). {v} is the integration variable.
INTEGRAL_RULE_TEXT = {
    "ConstantRule": "The integral of a constant c is c·{v}.",
    "ReciprocalRule": "Standard integral: ∫ 1/{v} d{v} = ln|{v}|.",
    "SinRule": "Standard integral: ∫ sin {v} d{v} = −cos {v}.",
    "CosRule": "Standard integral: ∫ cos {v} d{v} = sin {v}.",
    "Sec2Rule": "Standard integral: ∫ sec² {v} d{v} = tan {v}.",
    "Csc2Rule": "Standard integral: ∫ csc² {v} d{v} = −cot {v}.",
    "SecTanRule": "Standard integral: ∫ sec {v} tan {v} d{v} = sec {v}.",
    "CscCotRule": "Standard integral: ∫ csc {v} cot {v} d{v} = −csc {v}.",
    "SinhRule": "Standard integral: ∫ sinh {v} d{v} = cosh {v}.",
    "CoshRule": "Standard integral: ∫ cosh {v} d{v} = sinh {v}.",
    "ArctanRule": "This has the form 1/(a + b{v}²), whose integral is an arctangent.",
    "ArcsinRule": "Standard integral: ∫ 1/√(1 − {v}²) d{v} = arcsin {v}.",
    "ArcsinhRule": "Standard integral: ∫ 1/√(1 + {v}²) d{v} = arsinh {v}.",
    "ErfRule": "This Gaussian has no elementary antiderivative; it's written with the error function erf.",
    "SiRule": "This is the sine integral Si, a special function.",
    "CiRule": "This is the cosine integral Ci, a special function.",
    "ShiRule": "This is the hyperbolic sine integral Shi, a special function.",
    "ChiRule": "This is the hyperbolic cosine integral Chi, a special function.",
    "EiRule": "This is the exponential integral Ei, a special function.",
    "LiRule": "This is the logarithmic integral li, a special function.",
    "FresnelSRule": "This is a Fresnel integral, a special function.",
    "FresnelCRule": "This is a Fresnel integral, a special function.",
    "UpperGammaRule": "This is written with the incomplete gamma function, a special function.",
    "PolylogRule": "This is written with the polylogarithm, a special function.",
}


def integral_steps_for(f, x):
    """(antiderivative, steps) from SymPy's manual integrator, or raise NoSteps."""
    try:
        rule = mi.integral_steps(f, x)
        walker = _IntegralWalker()
        walker.walk(rule, 0)
        return rule.eval(), walker.steps
    except NoSteps:
        raise
    except Exception:  # an unexpected rule shape: no steps rather than a crash
        raise NoSteps


class _IntegralWalker:
    def __init__(self):
        self.steps = []
        self.names = {}  # substitution variables -> readable u, w, t

    def show(self, expr):
        return tex(expr.xreplace(self.names))

    def integrand(self, expr):
        return integrand_tex(expr.xreplace(self.names))

    def var(self, rule):
        return self.show(rule.variable)

    def times_d(self, factor, rule):
        """factor·dx, written as just dx when the factor is 1."""
        d = rf"d{self.var(rule)}"
        return d if factor == 1 else rf"{self.show(factor)}\, {d}"

    def equation(self, rule):
        return rf"\int {self.integrand(rule.integrand)}\, d{self.var(rule)} = {self.show(rule.eval())}"

    def walk(self, rule, depth):
        name = type(rule).__name__
        handler = getattr(self, "on_" + name, None)
        if isinstance(rule, mi.DontKnowRule):
            raise NoSteps
        if isinstance(rule, mi.AlternativeRule):
            return self.walk(rule.alternatives[0], depth)
        if handler:
            return handler(rule, depth)
        if isinstance(rule, mi.RewriteRule):  # includes CompleteSquareRule
            return self.on_RewriteRule(rule, depth)
        v = str(rule.variable.xreplace(self.names))
        text = INTEGRAL_RULE_TEXT.get(name, f"Apply the standard {name.removesuffix('Rule')} integral.")
        add(self.steps, depth, text.format(v=v), self.equation(rule))

    def on_AddRule(self, rule, depth):
        v = self.var(rule)
        split = " + ".join(rf"\int {self.integrand(s.integrand)}\, d{v}" for s in rule.substeps)
        add(self.steps, depth, "Integrate term by term.",
            rf"\int {self.integrand(rule.integrand)}\, d{v} = {split}")
        for sub in rule.substeps:
            self.walk(sub, depth + 1)
        add(self.steps, depth, "Add the results.", self.equation(rule))

    def on_ConstantTimesRule(self, rule, depth):
        v = self.var(rule)
        add(self.steps, depth, "Move the constant factor outside the integral.",
            rf"\int {self.integrand(rule.integrand)}\, d{v} = {self.show(rule.constant)} \int {self.integrand(rule.other)}\, d{v}")
        self.walk(rule.substep, depth + 1)
        add(self.steps, depth, "Multiply by the constant.", self.equation(rule))

    def on_PowerRule(self, rule, depth):
        v = str(rule.variable.xreplace(self.names))
        add(self.steps, depth, f"Power rule: ∫ {v}ⁿ d{v} = {v}ⁿ⁺¹/(n + 1), here with n = {rule.exp}.",
            self.equation(rule))

    def on_ExpRule(self, rule, depth):
        v = str(rule.variable.xreplace(self.names))
        if rule.base == S.Exp1:
            text = f"Standard integral: ∫ e^{v} d{v} = e^{v}."
        else:
            text = f"Exponential rule: ∫ a^{v} d{v} = a^{v} / ln a."
        add(self.steps, depth, text, self.equation(rule))

    def on_URule(self, rule, depth):
        u = Symbol("uwt"[min(len(self.names), 2)])
        self.names[rule.u_var] = u
        x = rule.variable
        du = diff(rule.u_func, x)
        add(self.steps, depth, "Substitute to simplify the integral:",
            rf"{tex(u)} = {self.show(rule.u_func)}, \quad d{tex(u)} = {self.times_d(du, rule)}")
        self.walk(rule.substep, depth + 1)
        add(self.steps, depth, f"Substitute back for {u}.", self.equation(rule))

    def on_PartsRule(self, rule, depth):
        x = rule.variable
        v_expr = rule.v_step.eval()
        du = diff(rule.u, x)
        dx = rf"\, d{self.var(rule)}"
        add(self.steps, depth, "Integrate by parts: ∫ u dv = u·v − ∫ v du, choosing",
            rf"u = {self.show(rule.u)}, \quad dv = {self.show(rule.dv)}{dx}"
            rf" \;\Rightarrow\; du = {self.times_d(du, rule)}, \quad v = {self.show(v_expr)}")
        add(self.steps, depth + 1, "Find v by integrating dv:", None)
        self.walk(rule.v_step, depth + 1)
        add(self.steps, depth, "So the integral becomes:",
            rf"\int {self.integrand(rule.integrand)}{dx} = {self.show(rule.u * v_expr)} - \int {self.integrand(v_expr * du)}{dx}")
        if rule.second_step is not None:
            self.walk(rule.second_step, depth + 1)
        add(self.steps, depth, "Put it together.", self.equation(rule))

    def on_CyclicPartsRule(self, rule, depth):
        add(self.steps, depth, "Integrate by parts twice; the original integral comes back on the right-hand side.", None)
        for parts in rule.parts_rules:
            add(self.steps, depth + 1, "By parts with",
                rf"u = {self.show(parts.u)}, \quad dv = {self.show(parts.dv)}\, d{self.var(rule)}")
        add(self.steps, depth, "Move that integral to the left-hand side and solve for it.", self.equation(rule))

    def on_RewriteRule(self, rule, depth):
        add(self.steps, depth, "Rewrite the integrand:",
            rf"{self.show(rule.integrand)} = {self.show(rule.rewritten)}")
        self.walk(rule.substep, depth + 1)

    def on_TrigSubstitutionRule(self, rule, depth):
        add(self.steps, depth, "Use a trigonometric substitution:",
            rf"{self.var(rule)} = {self.show(rule.func)}")
        add(self.steps, depth, "The integral becomes:", rf"\int {self.integrand(rule.rewritten)}\, d{tex(rule.theta)}")
        self.walk(rule.substep, depth + 1)
        add(self.steps, depth, "Substitute back.", self.equation(rule))


# -------------------------------------------------------------- derivatives

# d/du f(u) for the functions in the allowlist, and how to name them in a sentence.
DERIVATIVE_TABLE = {
    sin: (lambda u: cos(u), "sin"),
    cos: (lambda u: -sin(u), "cos"),
    tan: (lambda u: sec(u) ** 2, "tan"),
    exp: (lambda u: exp(u), "eˣ"),
    log: (lambda u: 1 / u, "ln"),
    asin: (lambda u: 1 / sqrt(1 - u ** 2), "arcsin"),
    acos: (lambda u: -1 / sqrt(1 - u ** 2), "arccos"),
    atan: (lambda u: 1 / (1 + u ** 2), "arctan"),
    sinh: (lambda u: cosh(u), "sinh"),
    cosh: (lambda u: sinh(u), "cosh"),
    tanh: (lambda u: 1 - tanh(u) ** 2, "tanh"),
    Abs: (lambda u: sign(u), "|x| (for x ≠ 0)"),
}


def derivative_steps(f, x):
    """(derivative, steps) using the usual rules one at a time, or raise NoSteps."""
    steps = []
    return _Differ(x, steps).d(f, 0), steps


class _Differ:
    def __init__(self, x, steps):
        self.x = x
        self.steps = steps

    def eq(self, expr, result):
        return rf"\frac{{d}}{{d{tex(self.x)}}}\left[{tex(expr)}\right] = {tex(result)}"

    def ddx(self, expr):  # unevaluated d/dx[expr], for "split into parts" lines
        return rf"\frac{{d}}{{d{tex(self.x)}}}\left[{tex(expr)}\right]"

    def d(self, expr, depth):
        x, steps = self.x, self.steps
        if not expr.has(x):
            add(steps, depth, "The derivative of a constant is 0.", self.eq(expr, S.Zero))
            return S.Zero
        if expr == x:
            add(steps, depth, "The derivative of x is 1.", self.eq(expr, S.One))
            return S.One
        if expr.is_Add:
            return self.sum_rule(expr, depth)
        if expr.is_Mul:
            return self.product_or_quotient(expr, depth)
        if expr.is_Pow:
            return self.power(expr, depth)
        if expr.func in DERIVATIVE_TABLE:
            return self.function(expr, depth)
        raise NoSteps

    def sum_rule(self, expr, depth):
        terms = expr.as_ordered_terms()
        add(self.steps, depth, "Sum rule: differentiate term by term.",
            rf"{self.ddx(expr)} = " + " + ".join(self.ddx(t) for t in terms))
        result = Add(*[self.d(t, depth + 1) for t in terms])
        add(self.steps, depth, "Add the results.", self.eq(expr, result))
        return result

    def product_or_quotient(self, expr, depth):
        x = self.x
        constant, rest = expr.as_independent(x, as_Add=False)
        if constant != 1:
            add(self.steps, depth, "Constant multiple rule: move the constant out.",
                rf"{self.ddx(expr)} = {tex(constant)} \cdot {self.ddx(rest)}")
            result = constant * self.d(rest, depth + 1)
            add(self.steps, depth, "Multiply by the constant.", self.eq(expr, result))
            return result
        num, den = fraction(expr, exact=True)
        if den != 1 and den.has(x):
            add(self.steps, depth, "Quotient rule: (u/v)′ = (u′·v − u·v′) / v², with",
                rf"u = {tex(num)}, \quad v = {tex(den)}")
            du = self.d(num, depth + 1)
            dv = self.d(den, depth + 1)
            result = (du * den - num * dv) / den ** 2
            add(self.steps, depth, "Put it into the quotient rule.", self.eq(expr, result))
            return result
        factors = expr.as_ordered_factors()
        u, v = factors[0], Mul(*factors[1:])
        add(self.steps, depth, "Product rule: (u·v)′ = u′·v + u·v′, with",
            rf"u = {tex(u)}, \quad v = {tex(v)}")
        du = self.d(u, depth + 1)
        dv = self.d(v, depth + 1)
        result = du * v + u * dv
        add(self.steps, depth, "Put it into the product rule.", self.eq(expr, result))
        return result

    def power(self, expr, depth):
        x = self.x
        base, n = expr.args
        if not n.has(x):
            if base == x:
                result = n * x ** (n - 1)
                add(self.steps, depth, f"Power rule: d/dx xⁿ = n·xⁿ⁻¹, here with n = {n}.", self.eq(expr, result))
                return result
            add(self.steps, depth, f"Chain rule with the power rule outside (n = {n}): d/dx uⁿ = n·uⁿ⁻¹·u′, with",
                rf"u = {tex(base)}")
            du = self.d(base, depth + 1)
            result = n * base ** (n - 1) * du
            add(self.steps, depth, "Multiply by u′.", self.eq(expr, result))
            return result
        if not base.has(x):
            add(self.steps, depth, "Exponential rule: d/dx aᵘ = aᵘ·ln(a)·u′, with",
                rf"a = {tex(base)}, \quad u = {tex(n)}")
            du = self.d(n, depth + 1)
            result = expr * log(base) * du
            add(self.steps, depth, "Multiply it out.", self.eq(expr, result))
            return result
        # x in both the base and the exponent, e.g. x^x
        add(self.steps, depth, "x is in both the base and the exponent, so use logarithmic differentiation:",
            rf"y = {tex(expr)} \;\Rightarrow\; \ln y = {tex(n * log(base))}")
        inner = self.d(n * log(base), depth + 1)
        result = expr * inner
        add(self.steps, depth, "Since y′/y = (ln y)′, multiply by y.", self.eq(expr, result))
        return result

    def function(self, expr, depth):
        x = self.x
        rule, name = DERIVATIVE_TABLE[expr.func]
        u = expr.args[0]
        if u == x:
            result = rule(x)
            add(self.steps, depth, f"Standard derivative of {name}.", self.eq(expr, result))
            return result
        U = Symbol("u")
        add(self.steps, depth, f"Chain rule: the derivative of {name} on the outside, times the derivative of the inside.",
            rf"u = {tex(u)}, \quad \frac{{d}}{{du}}{tex(expr.func(U))} = {tex(rule(U))}")
        du = self.d(u, depth + 1)
        result = rule(u) * du
        add(self.steps, depth, "Multiply by u′.", self.eq(expr, result))
        return result
