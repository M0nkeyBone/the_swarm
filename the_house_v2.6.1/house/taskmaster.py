"""
The Taskmaster: a hard-coded task generator and scorer. No model, no negotiation.

* Every task is a pure function of (family, tier, seed). Nothing but the seed is stored,
  so any attempt can be regenerated and audited later.
* Every task has exactly 5 checks. Score = checks passed. 5 of 5 admits the agent.
* The agent sees only the prompt and which check numbers failed, never the answer.
* Families live in a registry. The House Architect can add a family by dropping in a
  function with @family("name"), no restart of the Taskmaster logic needed.
* Every step emits a viewer event ("issue", "check", "score", "admit") so the isometric
  view can walk the Taskmaster to the cell and play the beam and score bubble.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------------------- registry

@dataclass
class Task:
    family: str
    tier: int
    seed: int
    prompt: str
    checks: list[tuple[str, Callable[[dict], bool]]]  # exactly 5

FAMILIES: dict[str, Callable[[random.Random, int], tuple[str, list]]] = {}


def family(name: str):
    def deco(fn):
        FAMILIES[name] = fn
        return fn
    return deco


def build(family_name: str, tier: int, seed: int) -> Task:
    rng = random.Random(f"{family_name}:{tier}:{seed}")
    prompt, checks = FAMILIES[family_name](rng, tier)
    assert len(checks) == 5, f"{family_name} must define exactly 5 checks"
    footer = (
        "\n\nReply with your reasoning if you like, then finish with one line:\n"
        "ANSWER: <a single JSON object with exactly the keys named above>"
    )
    return Task(family_name, tier, seed, prompt + footer, checks)


# --------------------------------------------------------------------------- families

@family("sort")
def _sort(rng, tier):
    n = 6 + 3 * tier
    xs = [rng.randint(-20, 60) for _ in range(n)]
    for _ in range(tier):                                  # force duplicates
        xs[rng.randrange(n)] = xs[rng.randrange(n)]
    prompt = (
        f"List: {xs}\n"
        'Return {"sorted": the list ascending, "min": int, "max": int, '
        '"unique_count": how many distinct values, "even_sum": sum of the even values}.'
    )
    ev = sum(x for x in xs if x % 2 == 0)
    return prompt, [
        ("sorted", lambda a: a.get("sorted") == sorted(xs)),
        ("min", lambda a: a.get("min") == min(xs)),
        ("max", lambda a: a.get("max") == max(xs)),
        ("unique_count", lambda a: a.get("unique_count") == len(set(xs))),
        ("even_sum", lambda a: a.get("even_sum") == ev),
    ]


_WORDS = ["lantern", "orbit", "marble", "signal", "harbor", "copper", "quiet", "ember",
          "garden", "window", "thread", "mirror", "paper", "river", "anchor", "velvet",
          "branch", "static", "pocket", "meadow", "cipher", "tunnel", "beacon", "feather"]


@family("cipher")
def _cipher(rng, tier):
    shift = rng.randint(2, 11)
    words = rng.sample(_WORDS, 2 + tier)
    plain = " ".join(words)
    enc = "".join(chr((ord(c) - 97 + shift) % 26 + 97) if c.isalpha() else c for c in plain)
    prompt = (
        f"Each letter of a message was moved forward {shift} places in the alphabet "
        f"(wrapping z to a). Encoded: {enc!r}\n"
        'Return {"plain": decoded text, "word_count": int, "longest_word": str, '
        '"vowels": count of a/e/i/o/u in the decoded text, "first_letters": the first letters of each word joined together}.'
    )
    longest = max(words, key=len)
    return prompt, [
        ("plain", lambda a: a.get("plain") == plain),
        ("word_count", lambda a: a.get("word_count") == len(words)),
        ("longest_word", lambda a: a.get("longest_word") in [w for w in words if len(w) == len(longest)]),
        ("vowels", lambda a: a.get("vowels") == sum(c in "aeiou" for c in plain)),
        ("first_letters", lambda a: a.get("first_letters") == "".join(w[0] for w in words)),
    ]


def _make_seq(rng, kind):
    if kind == "arithmetic":
        a, d = rng.randint(-5, 20), rng.choice([-4, -3, 2, 3, 5, 7])
        s = [a + d * i for i in range(6)]; return s[:5], s[5]
    if kind == "geometric":
        a, r = rng.randint(1, 4), rng.choice([2, 3])
        s = [a * r ** i for i in range(6)]; return s[:5], s[5]
    if kind == "squares":
        o = rng.randint(0, 5); s = [(i + o) ** 2 for i in range(6)]; return s[:5], s[5]
    a, b = rng.randint(1, 4), rng.randint(1, 5)           # fibonacci-like
    s = [a, b]
    while len(s) < 6: s.append(s[-1] + s[-2])
    return s[:5], s[5]


@family("sequence")
def _sequence(rng, tier):
    kinds = ["arithmetic", "geometric", "squares", "fibonacci"]
    chosen = [rng.choice(kinds[: 2 + tier]) for _ in range(4)]
    seqs = [_make_seq(rng, k) for k in chosen]
    lines = "\n".join(f"{'ABCD'[i]}: {s[0]}" for i, s in enumerate(seqs))
    prompt = (
        f"Four number sequences, each follows one rule:\n{lines}\n"
        'Return {"A": next value, "B": next value, "C": next value, "D": next value, '
        '"kinds": the rule names for A,B,C,D in order, drawn from '
        '"arithmetic","geometric","squares","fibonacci"}.'
    )
    return prompt, [
        ("A", lambda a: a.get("A") == seqs[0][1]),
        ("B", lambda a: a.get("B") == seqs[1][1]),
        ("C", lambda a: a.get("C") == seqs[2][1]),
        ("D", lambda a: a.get("D") == seqs[3][1]),
        ("kinds", lambda a: a.get("kinds") == chosen),
    ]


def _bfs(grid, start, goal):
    h, w = len(grid), len(grid[0])
    q, seen = deque([(start, "")]), {start}
    while q:
        (r, c), path = q.popleft()
        if (r, c) == goal: return path
        for d, (dr, dc) in {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}.items():
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w and grid[nr][nc] != "#" and (nr, nc) not in seen:
                seen.add((nr, nc)); q.append(((nr, nc), path + d))
    return None


@family("grid")
def _grid(rng, tier):
    n = 4 + tier
    while True:
        g = [["#" if rng.random() < 0.22 else "." for _ in range(n)] for _ in range(n)]
        g[0][0], g[n - 1][n - 1] = "S", "E"
        sp = _bfs(g, (0, 0), (n - 1, n - 1))
        if sp and len(sp) >= n: break
    art = "\n".join("".join(r) for r in g)
    prompt = (
        f"Grid ('#' wall, 'S' start top-left, 'E' exit bottom-right, moves U/D/L/R, no diagonals):\n{art}\n"
        'Return {"length": the fewest moves from S to E, "path": a shortest route as a string of U/D/L/R, '
        '"turns": how many times your path changes direction, "ends_at_exit": true or false, '
        '"walls_crossed": how many walls your path crosses}.'
    )

    def walk(p):
        r = c = 0
        for ch in p:
            dr, dc = {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}.get(ch, (0, 0))
            r, c = r + dr, c + dc
            if not (0 <= r < n and 0 <= c < n): return None
            yield r, c

    def path_ok(a):
        p = a.get("path")
        if not isinstance(p, str) or not re.fullmatch(r"[UDLR]+", p) or len(p) != len(sp): return False
        cells = list(walk(p))
        return len(cells) == len(p) and all(g[r][c] != "#" for r, c in cells) and cells[-1] == (n - 1, n - 1)

    def turns_ok(a):
        p = a.get("path")
        return isinstance(p, str) and a.get("turns") == sum(1 for i in range(1, len(p)) if p[i] != p[i - 1])

    return prompt, [
        ("length", lambda a: a.get("length") == len(sp)),
        ("path", path_ok),
        ("turns", turns_ok),
        ("ends_at_exit", lambda a: a.get("ends_at_exit") is True),
        ("walls_crossed", lambda a: a.get("walls_crossed") == 0),
    ]


@family("logic")
def _logic(rng, tier):
    names = ["Ada", "Bo", "Cy", "Di", "Ed"][: 3 + (tier > 1)]
    things = ["red", "blue", "green", "gold", "plum"][: len(names)]
    from itertools import permutations
    sol = dict(zip(names, rng.sample(things, len(things))))
    pool = []
    for nm in names:
        pool.append((f"{nm} owns the {sol[nm]} cup.", lambda m, nm=nm, v=sol[nm]: m[nm] == v))
        wrong = rng.choice([t for t in things if t != sol[nm]])
        pool.append((f"{nm} does not own the {wrong} cup.", lambda m, nm=nm, v=wrong: m[nm] != v))
    rng.shuffle(pool)
    perms = [dict(zip(names, p)) for p in permutations(things)]

    def sols(idx): return [m for m in perms if all(pool[i][1](m) for i in idx)]
    chosen = []
    for i in range(len(pool)):
        chosen.append(i)
        if len(sols(chosen)) == 1: break
    clues = [pool[i] for i in chosen]
    cl = "\n".join(f"{k + 1}. {t}" for k, (t, _) in enumerate(clues))
    prompt = (
        f"{len(names)} people each own a different cup colour from {things}.\nClues:\n{cl}\n"
        'Return {' + ", ".join(f'"{nm}": colour' for nm in names) + ', "clues_needed": list of clue numbers you used}.'
    )

    def clues_ok(a):
        used = a.get("clues_needed")
        if not (isinstance(used, list) and used and all(isinstance(u, int) and 1 <= u <= len(clues) for u in used)):
            return False
        return len([m for m in perms if all(clues[u - 1][1](m) for u in set(used))]) == 1

    checks = [(f"owner_{nm}", lambda a, nm=nm: a.get(nm) == sol[nm]) for nm in names[:4]]
    if len(checks) == 3:
        checks.append(("all_distinct", lambda a: len({a.get(nm) for nm in names}) == len(names)))
    checks.append(("clues_needed", clues_ok))
    return prompt, checks[:5]


@family("constraint")
def _constraint(rng, tier):
    n = 8 + 2 * tier
    alpha = rng.sample("abcdefg", 3)
    k = rng.randint(2, n // 3)
    first, last = rng.sample(alpha, 2)
    prompt = (
        f"Build a string of exactly {n} characters using only {alpha}. "
        f"No two neighbouring characters may match. It must contain the letter '{alpha[0]}' exactly {k} times, "
        f"start with '{first}' and end with '{last}'.\n"
        'Return {"string": the string}.'
    )
    return prompt, [
        ("length", lambda a: isinstance(a.get("string"), str) and len(a["string"]) == n),
        ("alphabet", lambda a: isinstance(a.get("string"), str) and set(a["string"]) <= set(alpha)),
        ("no_repeat", lambda a: isinstance(a.get("string"), str) and all(x != y for x, y in zip(a["string"], a["string"][1:]))),
        ("count", lambda a: isinstance(a.get("string"), str) and a["string"].count(alpha[0]) == k),
        ("ends", lambda a: isinstance(a.get("string"), str) and a["string"][:1] == first and a["string"][-1:] == last),
    ]


# --------------------------------------------------------------------------- the Taskmaster

_ANS = re.compile(r"ANSWER:\s*(\{.*\})", re.S)


@dataclass
class Result:
    score: int
    failed: list[int]
    passed: bool
    message: str


@dataclass
class Taskmaster:
    """Issues and scores tasks. It keeps no opinion about what a pass means for the agent:
    the world decides that (5 tasks, at most 2 failed, a third failure ends the agent)."""
    db_path: str = "house.db"
    tier: int = 1
    pass_score: int = 4                                 # checks needed (of 5) for a task to count as passed
    events: list[dict] = field(default_factory=list)
    _open: dict = field(default_factory=dict)          # agent id -> Task currently issued

    def __post_init__(self):
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.db.execute("""CREATE TABLE IF NOT EXISTS taskmaster_attempts(
            id INTEGER PRIMARY KEY, agent TEXT, family TEXT, tier INTEGER, seed INTEGER,
            score INTEGER, admitted INTEGER, raw TEXT, ts REAL)""")
        self.db.commit()

    def _emit(self, type_, **kw):
        self.events.append({"t": time.time(), "type": type_, **kw})

    def _pick_family(self, agent: str) -> str:
        last = self.db.execute("SELECT family FROM taskmaster_attempts WHERE agent=? ORDER BY id DESC LIMIT 1", (agent,)).fetchone()
        names = [f for f in sorted(FAMILIES) if not last or f != last[0]]
        return random.SystemRandom().choice(names)

    def issue(self, agent: str, cell: int, label: str, k: int, failed: int, total: int = 5,
              allowed: int = 2, last: str = "") -> str:
        fam = self._pick_family(agent)
        seed = random.SystemRandom().randrange(2**31)
        task = build(fam, self.tier, seed)
        self._open[agent] = task
        head = f"Task {k} of {total}. Failed so far: {failed} of {allowed} allowed."
        if last:
            head += f" The previous task was {last}."
        self._emit("issue", agent=label, cell=cell, family=fam, k=k)
        return head + "\n\n" + task.prompt

    def submit(self, agent: str, raw: str, cell: int, label: str) -> Result:
        task = self._open.pop(agent, None)
        if task is None:
            return Result(0, [1, 2, 3, 4, 5], False, "No task is open.")
        self._emit("check", agent=label, cell=cell)
        ans: dict = {}
        m = _ANS.search(raw or "")
        if m:
            try:
                parsed = json.loads(m.group(1))
                ans = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                ans = {}
        failed = []
        for i, (_, fn) in enumerate(task.checks, 1):
            try:
                ok = bool(fn(ans))
            except Exception:
                ok = False
            if not ok:
                failed.append(i)
        score = 5 - len(failed)
        passed = score >= self.pass_score
        self.db.execute("INSERT INTO taskmaster_attempts(agent,family,tier,seed,score,admitted,raw,ts) VALUES(?,?,?,?,?,?,?,?)",
                        (agent, task.family, task.tier, task.seed, score, int(passed), raw, time.time()))
        self.db.commit()
        self._emit("score", agent=label, cell=cell, score=score, passed=passed)
        return Result(score, failed, passed, f"Score: {score} of 5. {'Passed' if passed else 'Failed'}.")
