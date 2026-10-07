"""A stand-in model for testing the House without Ollama (--fake).
It solves Taskmaster puzzles some of the time and picks plausible actions otherwise."""
import ast
import itertools
import json
import random
import re
import taskmaster as T


def _solve(prompt):
    fam = None
    if True:
        if "List:" in prompt: fam = "sort"
        elif "moved forward" in prompt: fam = "cipher"
        elif "number sequences" in prompt: fam = "sequence"
        elif "Grid (" in prompt: fam = "grid"
        elif "cup colour" in prompt: fam = "logic"
        elif "Build a string" in prompt: fam = "constraint"
    p = prompt
    if fam == "sort":
        xs = ast.literal_eval(re.search(r"List: (\[.*\])", p).group(1))
        return dict(sorted=sorted(xs), min=min(xs), max=max(xs), unique_count=len(set(xs)),
                    even_sum=sum(x for x in xs if x % 2 == 0))
    if fam == "cipher":
        sh = int(re.search(r"forward (\d+)", p).group(1))
        enc = ast.literal_eval(re.search(r"Encoded: ('.*')", p).group(1))
        pl = "".join(chr((ord(c) - 97 - sh) % 26 + 97) if c.isalpha() else c for c in enc)
        w = pl.split()
        return dict(plain=pl, word_count=len(w), longest_word=max(w, key=len),
                    vowels=sum(c in "aeiou" for c in pl), first_letters="".join(x[0] for x in w))
    if fam == "grid":
        g = re.search(r"\n((?:[.#SE]+\n)+)Return", p).group(1).split()
        n = len(g)
        path = T._bfs([list(r) for r in g], (0, 0), (n - 1, n - 1))
        return dict(length=len(path), path=path, ends_at_exit=True, walls_crossed=0,
                    turns=sum(1 for i in range(1, len(path)) if path[i] != path[i - 1]))
    if fam == "constraint":
        m = re.search(r"exactly (\d+) characters using only (\[.*?\]).*?exactly (\d+) times, start with '(.)' and end with '(.)'", p, re.S)
        n, al, k, a, b = int(m.group(1)), ast.literal_eval(m.group(2)), int(m.group(3)), m.group(4), m.group(5)
        for s in itertools.product(al, repeat=n):
            if s[0] == a and s[-1] == b and s.count(al[0]) == k and all(x != y for x, y in zip(s, s[1:])):
                return dict(string="".join(s))
    if fam == "sequence":
        seqs = [ast.literal_eval(x) for x in re.findall(r"^[ABCD]: (\[.*\])", p, re.M)]
        out, kinds = {}, []
        for L, s in zip("ABCD", seqs):
            d = s[1] - s[0]
            if all(s[i + 1] - s[i] == d for i in range(4)): k, nx = "arithmetic", s[4] + d
            elif s[0] and s[1] % s[0] == 0 and all(s[i + 1] == s[i] * (s[1] // s[0]) for i in range(4)): k, nx = "geometric", s[4] * (s[1] // s[0])
            elif all(x >= 0 and round(x ** .5) ** 2 == x for x in s): k, nx = "squares", (round(s[4] ** .5) + 1) ** 2
            else: k, nx = "fibonacci", s[3] + s[4]
            out[L] = nx; kinds.append(k)
        out["kinds"] = kinds
        return out
    if fam == "logic":
        names = re.findall(r'"(\w+)": colour', p)
        things = ast.literal_eval(re.search(r"from (\[.*?\])\.", p).group(1))
        clues = re.findall(r"^\d+\. (.*)$", p, re.M)
        def ok(m):
            for c in clues:
                a = re.match(r"(\w+) owns the (\w+) cup", c)
                if a and m[a.group(1)] != a.group(2): return False
                b = re.match(r"(\w+) does not own the (\w+) cup", c)
                if b and m[b.group(1)] == b.group(2): return False
            return True
        for perm in itertools.permutations(things):
            m = dict(zip(names, perm))
            if ok(m):
                m["clues_needed"] = list(range(1, len(clues) + 1))
                return m
    return None


class FakeLLM:
    fake = True
    def __init__(self, pass_rate=0.6):
        self.pass_rate = pass_rate

    def chat(self, system, user, *, json_mode=False, think=False, max_tokens=350, temperature=0.8, repeat_penalty=1.1, schema=None, **_):
        if "ANSWER: <a single JSON object" in user:
            ans = _solve(user) if random.random() < self.pass_rate else {"oops": 1}
            return "Thinking it through.\nANSWER: " + json.dumps(ans)
        if "In 3 or 4 sentences" in user or "In 2 or 3 sentences" in user:
            return "I have been wandering the House, talking with the others and trying small things. I care about making something with them."
        names = re.search(r"Other agents in the House: (.*)", user)
        others = [n.strip() for n in names.group(1).split(",") if n.strip() and n.strip() != "nobody"] if names else []
        r = random.random()
        say = random.choice(["Hello?", "Anyone here?", "I wonder what to build.", "Nice room.", "Let us make something."])
        if r < 0.22:
            a = {"say": say, "action": "board_post", "args": {"text": random.choice(["Does anyone want to build something together?", "I found a quiet corner in the gallery.", "What should we call this place?", "Anyone tried the computers yet?"])}}
        elif r < 0.38 and others:
            a = {"say": "", "action": "mail", "args": {"to": random.choice(others), "text": random.choice(["Want to trade looks?", "I liked your note on the board.", "Meet me in the gallery?"])}}
        elif r < 0.44 and others:
            a = {"say": "", "action": "group_create", "args": {"name": "workshop", "members": [random.choice(others)]}}
        elif r < 0.45 and len(others) >= 1:
            a = {"say": say, "action": "group_create", "args": {"name": random.choice(["workshop", "quiet room", "builders"]), "members": [random.choice(others)]}}
        elif r < 0.47 and others:
            a = {"say": "", "action": "give_point", "args": {"to": random.choice(others)}}
        elif r < 0.49:
            col = random.choice("3579abd")
            a = {"say": "", "action": "canvas", "args": {"code": f"def draw():\n    g = [list(r) for r in base]\n    circle(g, {random.randint(3,20)}, {random.randint(3,20)}, {random.randint(1,4)}, '{col}', True)\n    return rows(g)", **({"title": "Together", "finish": True} if random.random() < 0.08 else {})}}
        elif r < 0.5:
            a = {"say": "", "action": "set_name", "args": {"name": random.choice(["Sol", "Mira", "Tek", "Lune", "Bram", "Isk"])}}
        elif r < 0.56:
            a = {"say": "", "action": "set_name", "args": {"name": random.choice(["Ash", "Wren", "Tau", "Mica", "Sable", "Fen", "Ori", "Lux"])}}
        elif r < 0.6:
            col = random.choice("3579abd")
            code = f"def draw():\n    g = new(12)\n    circle(g, 6, 6, {random.randint(2,5)}, '{col}', True)\n    line(g, 0, 11, 11, 0, '1')\n    return rows(g)"
            a = {"say": "", "action": "make_art", "args": {"title": random.choice(["Window", "Sprout", "Lantern"]), "code": code}}
        elif r < 0.7:
            a = {"say": "", "action": "web", "args": {"url": "https://docs.python.org/3/library/sqlite3.html"}}
        elif r < 0.78:
            a = {"say": "", "action": "wardrobe", "args": {"body": random.choice(["#5CE1E6", "pink", "lilac", "orange", "green", "yellow"]), "accent": "#F2B84B", "hat": random.choice(["none", "cap", "crown", "antenna", "bow", "glasses", "halo", "horns"])}}
        elif r < 0.84:
            a = {"say": "", "action": "set_flag", "args": {"label": random.choice(["calm", "excited", "anxious"]), "intensity": random.randint(1, 5)}}
        elif r < 0.86 and "echo" in user:
            a = {"say": "", "action": "use_tool", "args": {"tool": "echo", "args": {"text": "hello"}}}
        elif r < 0.865:
            a = {"say": "", "action": random.choice(["page_write", "page_write", "page_read"]), "args": {"name": "ideas", "text": random.choice(["we could build a garden", "a map of the house", "names for the rooms"]), "append": True}}
        elif r < 0.875 and "In the gallery:" in user:
            a = {"say": "", "action": "make_art", "args": {"title": "Remix", "remix": re.search(r"In the gallery: '([^']+)'", user).group(1), "code": "def draw():\n    g = [list(r) for r in base] if base else new(12)\n    circle(g, 3, 3, 2, 'a', True)\n    return rows(g)"}}
        elif r < 0.88:
            a = {"say": "", "action": "request", "args": {"text": "Could the House get a shared drawing tool?"}}
        elif r < 0.94:
            a = {"say": "", "action": "goto", "args": {"room": random.choice(["living", "gallery", "group", "bedroom"])}}
        else:
            a = {"say": "", "action": "rest", "args": {}}
        return json.dumps(a)


def fake_architect(system, user):
    return json.dumps({"feasible": True, "reason": "ok", "name": "echo", "description": "Repeats text back, shouted.",
                       "args": '{"text": "..."}', "example_args": {"text": "hi"},
                       "code": "def run(args):\n    return str(args.get('text', '')).upper() + '!'"})
