"""Runs small Python programs written by agents (pixel art) or by the architect (tools).

Each program runs in its own short-lived Python process with: an allowlist of imports, no file or
network access from the language level (open/exec/eval/__import__ and dunder access are rejected),
a few seconds of time, and on Linux/macOS a memory cap. It is a guard against mistakes and
mischief by a language model, not a hardened sandbox for hostile humans: only run code you are
comfortable with on your own machine."""
import ast
import re
import json
import subprocess
import sys

ALLOWED = {"math", "random", "re", "json", "datetime", "itertools", "collections", "string",
           "statistics", "textwrap", "functools", "colorsys"}
BAD_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "locals",
             "vars", "getattr", "setattr", "delattr", "memoryview", "help", "exit", "quit", "dir", "type"}

HELPERS = '''
def new(n=16, c="0"):
    return [[c] * n for _ in range(n)]
def px(g, x, y, c, *_, **__):
    if 0 <= y < len(g) and 0 <= x < len(g[0]):
        g[y][x] = c
def rect(g, x, y, w, h, c, fill=True, *_, **__):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            if fill or yy in (y, y + h - 1) or xx in (x, x + w - 1):
                px(g, xx, yy, c)
def line(g, x0, y0, x1, y1, c, *_, **__):
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        px(g, x0, y0, c)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy; x0 += sx
        if e2 <= dx:
            err += dx; y0 += sy
def circle(g, cx, cy, r, c, fill=False, *_, **__):
    for yy in range(cy - r, cy + r + 1):
        for xx in range(cx - r, cx + r + 1):
            d = (xx - cx) ** 2 + (yy - cy) ** 2
            if d <= r * r + r * 0.8 and (fill or d >= (r - 1) ** 2):
                px(g, xx, yy, c)
def rows(g):
    return [list(r) for r in g]      # rows are lists of characters, so g[y][x] = c still works on the result
def fill(g, c):
    for r in g:
        for i in range(len(r)):
            r[i] = c
    return g
def hline(g, x, y, w, c):
    rect(g, x, y, w, 1, c)
def vline(g, x, y, h, c):
    rect(g, x, y, 1, h, c)
def clamp(v, lo, hi):
    return max(lo, min(hi, v))
'''

TEMPLATE = """def draw():
    g = base            # the current grid: a list of rows, each a list of characters; g[y][x]
    rect(g, 2, 2, 6, 6, '5')
    circle(g, 12, 12, 4, '7', fill=True)
    for i in range(20):
        px(g, random.randint(0, N - 1), random.randint(0, N - 1), 'd')   # random is ready to use; N is the grid size
    return rows(g)"""

HELPER_DOC = ("Helpers, already available (no import needed): new(n, c) grid of n by n; px(g,x,y,c); rect(g,x,y,w,h,c); line(g,x0,y0,x1,y1,c); "
              "circle(g,cx,cy,r,c,fill=True); fill(g,c); hline(g,x,y,w,c); vline(g,x,y,h,c); rows(g) turns the grid into the answer; "
              "base is the grid as it is now; N is its size; random and math are ready. Colours are the characters 0-9 and a-f: 0 dark, 1 white, 2 grey, 3 cyan, 4 teal, 5 green, 6 dark green, 7 yellow, 8 gold, 9 orange, a pink, b lilac, c purple, d blue, e dark blue, f brown. "
              "g[y][x] is a character; use px() so you cannot go out of bounds. Your code must define draw() and end with return rows(g). "
              "A working example:\n" + TEMPLATE)


def clean_code(code):
    """Agents often wrap their code in markdown, JSON or pseudo-calls. Strip that off."""
    c = str(code or "").replace("\r", "")
    if "\n" not in c and "\\n" in c:
        c = c.replace("\\n", "\n")
    c = re.sub(r"^\s*```(?:python)?\s*", "", c)
    c = re.sub(r"\s*```\s*$", "", c)
    lines = c.split("\n")
    out = []
    for ln in lines:
        if re.match(r"^\s*[\"']?(finish|title|args|action)[\"']?\s*:", ln) or re.match(r"^\s*canvas\s*[\{\(]", ln) or re.match(r"^\s*[\"']?code[\"']?\s*:\s*[\{\"']?\s*$", ln):
            continue
        out.append(ln)
    c = "\n".join(out)
    k = c.rfind("return rows(")
    if k >= 0:        # drop pseudo-call junk after the final return rows(...)
        depth, e = 0, k + len("return rows")
        for p in range(e, len(c)):
            depth += (c[p] == "(") - (c[p] == ")")
            if depth == 0:
                e = p + 1
                break
        c = c[:e]
    return c.strip("\n")


def hint(err):
    """A plain-language next step for the commonest mistakes."""
    e = str(err)
    if "'module' object is not callable" in e:
        return "random is a module: write random.random() or random.randint(0, 5), not random()."
    m = re.search(r"NameError: name '(\w+)' is not defined", e)
    if m:
        n = m.group(1)
        if n in ("random", "math"):
            return f"Put import {n} at the top, or just use {n} (it is already available)."
        if n in ("base", "N"):
            return "base and N exist only as plain names inside draw(): use g = base."
        return f"'{n}' is not a helper. The helpers are: new, px, rect, line, circle, fill, hline, vline, rows, clamp. Define it yourself with def {n}(...) above draw()."
    if "'str' object does not support item assignment" in e:
        return "A string cannot be changed in place. Make the grid with g = new(N, '0') (or g = base) and change cells with px(g, x, y, c); only call rows(g) at the very end."
    if "IndexError" in e:
        return "A grid is g[y][x] with y and x from 0 to N-1. Use px(g, x, y, c): it ignores positions outside the grid."
    if "unterminated string" in e or "line continuation" in e or "syntax error" in e:
        return "Send only the Python code in the code field: no canvas { } or JSON wrapper, no finish/title inside it. Use real line breaks."
    if "must return" in e or "rows" in e:
        return "End draw() with return rows(g)."
    if "positional argument" in e:
        return "Check the helper's arguments: circle(g, cx, cy, r, c, fill=True), rect(g, x, y, w, h, c), line(g, x0, y0, x1, y1, c), px(g, x, y, c)."
    return ""


RUNNER = r'''
import sys, json, builtins
req = json.loads(sys.stdin.read())
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (600 * 2**20, 600 * 2**20))
except Exception:
    pass
ALLOWED = set(req["allowed"])
BAD = set(req["bad"])
real_import = builtins.__import__
def safe_import(name, g=None, l=None, fromlist=(), level=0):
    if name.split(".")[0] not in ALLOWED:
        raise ImportError("import not allowed: " + name)
    return real_import(name, g, l, fromlist, level)
safe = {k: v for k, v in vars(builtins).items() if k not in BAD}
safe["__import__"] = safe_import
import random as _r, math as _m
class _RM:
    def __call__(self):
        return _r.random()
    def __getattr__(self, k):
        return getattr(_r, k)
RM = _RM()
_ri = safe_import
def safe_import(name, g=None, l=None, fromlist=(), level=0):
    if name == "random" and not fromlist:
        return RM
    return _ri(name, g, l, fromlist, level)
safe["__import__"] = safe_import
g = {"__builtins__": safe, "__name__": "agent_code", "random": RM, "math": _m}
try:
    exec(req["helpers"], g)
    exec(compile(req["code"], "agent_code", "exec"), g)
    fn = g[req["entry"]]
    args = req["args"]
    if not args:      # agents often write draw(base) or draw(g): pass the canvas in instead of failing
        import inspect as _i
        need = sum(1 for p in _i.signature(fn).parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        args = [g.get("base")] * need
    out = fn(*args)
    print("@@RESULT@@" + json.dumps({"ok": True, "value": out}))
except BaseException as e:
    import traceback
    ln = [f.lineno for f in traceback.extract_tb(e.__traceback__) if f.filename == "agent_code"]
    print("@@RESULT@@" + json.dumps({"ok": False, "error": type(e).__name__ + ": " + str(e)[:300] + (" (line %d)" % ln[-1] if ln else "")}))
'''


def repair(code):
    """Models often lose indentation (or mix tabs). If the code does not parse, try to put the indentation back."""
    try:
        ast.parse(code)
        return code
    except SyntaxError:
        pass
    c = code.replace("\t", "    ")
    try:
        ast.parse(c)
        return c
    except SyntaxError:
        pass
    src = [l.rstrip() for l in c.split("\n")]
    out, stack, prev, prev_colon, body = [], [], None, False, None
    for ln in src:
        if not ln.strip():
            out.append("")
            continue
        o = len(ln) - len(ln.lstrip())
        t = ln.strip()
        if re.match(r"(def|class)\b", t) and not stack:
            new = 0
            stack = [(o, 0)]
        elif prev_colon:
            base = stack[-1][1] if stack else 0
            new = base + 4
            stack.append((o, new))
        else:
            while len(stack) > 1 and stack[-1][0] > o:
                stack.pop()
            if stack and stack[-1][0] == o:
                new = stack[-1][1]
            elif stack:
                new = stack[-1][1]
            else:
                new = 0
            if re.match(r"(else|elif|except|finally)\b", t) and len(stack) > 1 and prev is not None:
                # an else/elif belongs to the block one level out from the body it follows
                new = max(0, stack[-1][1] - 4)
                if len(stack) > 1:
                    stack.pop()
            if re.match(r"return\b", t) and o == 0 and len(stack) > 1:
                # a flattened return belongs at function level, not inside the last loop
                new = 4
                del stack[2:]
        out.append(" " * new + t)
        prev, prev_colon = t, t.endswith(":")
    r = "\n".join(out)
    try:
        ast.parse(r)
        return r
    except SyntaxError:
        return code


def check(code):
    """Raise ValueError if the code uses anything not allowed."""
    if len(code) > 6000:
        raise ValueError("code is too long (6000 characters at most)")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e.msg} (line {e.lineno})")
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] not in ALLOWED:
                    raise ValueError(f"import not allowed: {a.name}")
        elif isinstance(n, ast.ImportFrom):
            if n.level or (n.module or "").split(".")[0] not in ALLOWED:
                raise ValueError(f"import not allowed: {n.module}")
        elif isinstance(n, ast.Name) and (n.id in BAD_NAMES or n.id.startswith("__")):
            raise ValueError(f"name not allowed: {n.id}")
        elif isinstance(n, ast.Attribute) and n.attr.startswith("__"):
            raise ValueError(f"attribute not allowed: {n.attr}")


def run(code, entry, args=(), timeout=4.0, helpers=True, prelude=""):
    """Returns (ok, value_or_error_message)."""
    code = repair(code)
    try:
        check(code)
    except ValueError as e:
        return False, str(e)
    req = {"code": code, "entry": entry, "args": list(args), "allowed": sorted(ALLOWED), "bad": sorted(BAD_NAMES),
           "helpers": (HELPERS if helpers else "") + prelude}
    try:
        p = subprocess.run([sys.executable, "-I", "-c", RUNNER], input=json.dumps(req), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"it ran longer than {timeout:g} seconds"
    out = p.stdout.rsplit("@@RESULT@@", 1)
    if len(out) != 2:
        return False, (p.stderr.strip().splitlines() or ["it did not finish"])[-1][:300]
    try:
        r = json.loads(out[1])
    except Exception:
        return False, "its result could not be read"
    return (True, r["value"]) if r.get("ok") else (False, r.get("error", "error"))


def run_art(code, base=None, size=None):
    """Run an agent's art program. Returns (ok, rows_or_error)."""
    ok, v = run(code, "draw", prelude="\nbase = " + (repr([list(r) for r in base]) if base else "new(%d, '0')" % (size or 12)) + "\nN = len(base)\n")
    if not ok:
        return False, v
    HEX = "0123456789abcdef"
    if not isinstance(v, list) or not v:
        return False, "draw() must return the grid: use return rows(g) at the end"
    try:     # be forgiving: accept lists of lists, '.' or odd characters (become 0), and the wrong size (resampled)
        g = [(r if isinstance(r, str) else "".join(str(c)[:1] for c in r)).lower() for r in v]
    except TypeError:
        return False, "draw() must return a list of rows (use return rows(g))"
    n = len(g)
    m = max(len(r) for r in g)
    if not (2 <= n <= 200) or m < 2 or m > 400:
        return False, f"draw() returned {n} rows; it should return a grid of rows, for example 24 rows of 24 characters"
    g = ["".join(c if c in HEX else "0" for c in r).ljust(m, "0") for r in g]      # ragged or non-square grids are padded, then resampled
    want = size if size else min(16, max(8, n))
    if n != want or m != want:
        g = ["".join(g[y * n // want][x * m // want] for x in range(want)) for y in range(want)]
    return True, g


def run_outfit(code):
    """Run an agent's clothing program. Returns (ok, 12 rows of 12 chars or an error)."""
    ok, v = run(code, "draw")
    if not ok:
        return False, v
    if isinstance(v, list):
        v = ["".join(map(str, r)) if isinstance(r, list) else r for r in v]
    if not (isinstance(v, list) and len(v) == 12 and all(isinstance(r, str) and len(r) == 12 and all(c in "0123456789abcdefABCDEF." for c in r) for r in v)):
        return False, "draw() must return exactly 12 strings of 12 characters; use '.' for see-through and 0-9, a-f for colours"
    return True, [r.lower() for r in v]
