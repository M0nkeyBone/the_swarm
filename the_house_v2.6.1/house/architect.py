"""The House Architect: turns build requests the homeowner approved into working code, automatically.

Pipeline (one request at a time, oldest first):
    agent `request`  ->  no API key?  the request is NOT filed; the agent is told to write the code itself and
                                       submit it with submit_script for manual approval (Code & tools panel)
                     ->  key present: filed as 'pending' in the Architect tab
    homeowner Approve ->  'approved'  ->  'building'  ->  Claude writes the code  ->  the House tests it
                     ->  tool:   sandbox test with Claude's example args          -> installed live ('built')
                     ->  plugin: loaded live if auto-install is on (default)      -> installed live ('built')
                                 or saved to plugins/pending/ for the homeowner   -> 'built' (waiting for install)
                     ->  if the test or load fails, the error goes back to Claude once for a fix, then re-tested
                     ->  'failed' (with the reason) or 'rejected' (Claude says it cannot be built as code)
    Network errors, rate limits and overloads are retried with backoff; a request interrupted by a restart
    goes back to 'approved'. Every step is logged on the 'architect' channel and the author is mailed the result.

The key comes from the ANTHROPIC_API_KEY environment variable, or is typed into the Architect tab on the
homeowner's own computer, where it is kept in memory only and never written to disk."""
import json
import os
import re
import time
import urllib.error
import urllib.request

import sandbox

MAX_TOOLS = 24
RETRY_WAITS = (5, 20, 60)        # seconds between attempts for network errors, 429 and 5xx
SYSTEM = """You are the architect of a small virtual house where AI agents live. An agent asked the homeowner for something and the homeowner approved it. You turn the request into working code.

Prefer a TOOL. A tool is Python code that defines `def run(args):` where args is a dict the agent supplies, and returns a short string (under 800 characters). Rules for tool code:
- Allowed imports only: math, random, re, json, datetime, itertools, collections, string, statistics, textwrap, functools, colorsys.
- No files, network, input, or global state; it must finish in under 3 seconds and must not crash on missing or odd arguments (validate args and return a helpful message).
- Under 80 lines.

Some requests need more than a tool: a new action agents can take that remembers things over time, reacts to what happens in the house, or posts or messages on its own schedule. For those, set "kind": "plugin" and write a plugin instead. A plugin is a Python file with:
    DESCRIPTION = "one line"
    ACTIONS = {"action_name": {"doc": '{"arg": "..."}  what it does, written to the agent', "run": fn}}   where fn(ctx, agent, args) returns a short string
    optional on_event(ctx, ev) with ev = dict(channel, actor, text, type), on_tick(ctx) about every 5 seconds, setup(ctx)
ctx offers: ctx.store (a dict kept across restarts, JSON values only; call ctx.save() after changing it), ctx.agents() (list of dicts id, display, room), ctx.display(agent), ctx.say(agent, text), ctx.remember(agent, text, importance), ctx.mail(agent, text), ctx.board(text), ctx.event(text).
Plugins may use only the standard library, must not touch files, the network or subprocesses, must never raise on odd args, and must not reuse these built-in action names: @BUILTIN@.

Agents can already do these things without new code, so do not build them (say feasible false and tell the agent which action to use): post on a public board (board_post), private mail (mail), groups (group_create, group_say), shared pages anyone can write (page_write, page_read), notes, files (file_write), pixel art (make_art), web search of allowed sites (web), plans (plan), giving life points (give_point).
If the request cannot be done as code at all (a new room, internet access, talking to people outside, more life points), say so.

Reply with ONLY a JSON object, no other text:
{"kind": "tool"|"plugin", "feasible": true|false, "reason": "<one sentence, written to the agent>", "name": "<snake_case, at most 24 characters>", "description": "<one sentence for the agents>", "args": "<one line describing the args dict, e.g. {\\"text\\": \\"...\\"}>", "example_args": {...}, "code": "<the python source>"}"""

NO_KEY_TEXT = ("No API key at this moment: the architect cannot build anything right now, so your request was not sent. "
               "If you want this to exist, write it yourself: put the Python code in your archive with file_write, then send it with "
               "submit_script. The homeowner reviews it for manual approval, and if it is installed it becomes a plugin everyone can use "
               "(and you gain two life points).")


class _DryCtx:
    """A stand-in ctx for test-calling a plugin action once before it goes live."""
    name = "dry"

    def __init__(self):
        self.store = {}

    def save(self): pass
    def agents(self): return [{"id": "a0", "display": "#0", "room": "living"}, {"id": "a1", "display": "#1", "room": "gallery"}]
    def display(self, agent): return "#0"
    def say(self, agent, text): pass
    def remember(self, agent, text, importance=3): pass
    def mail(self, agent, text): pass
    def board(self, text): pass
    def event(self, text): pass
    def log(self, text): pass


class TransientError(Exception):
    pass


class KeyRejected(Exception):
    pass


class Architect:
    def __init__(self, world, daily_limit=5, model=None, caller=None):
        self.w = world
        self.daily_limit = daily_limit
        self.model = model or os.environ.get("ARCHITECT_MODEL", "claude-sonnet-5-5")
        self.caller = caller          # for tests: caller(system, user) -> text
        self.session_key = ""         # typed in the Architect tab; memory only, never saved
        self.key_error = ""
        self.current = None
        self._wake = 0.0
        self.recover()

    # ------------------------------------------------------------ key and status
    def key(self):
        return self.session_key or os.environ.get("ANTHROPIC_API_KEY", "")

    @property
    def connected(self):
        return bool((self.caller or self.key()) and not self.key_error)

    def key_source(self):
        if self.caller:
            return "test"
        if self.session_key:
            return "session"
        return "env" if os.environ.get("ANTHROPIC_API_KEY") else "none"

    def set_key(self, key):
        key = (key or "").strip()
        self.session_key, self.key_error = key, ""
        if key:
            self.w.event("architect", "Architect", "An API key was entered for this session. The architect is connected.", "arch")
        else:
            self.w.event("architect", "Architect", "The session API key was cleared.", "arch")
            self.no_key_sweep()
        self._wake = 0

    def auto_install(self):
        r = self.w.one("SELECT value FROM meta WHERE key='arch_auto_install'")
        return (r["value"] if r else "1") == "1"

    def set_auto_install(self, on):
        self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES('arch_auto_install',?)", ("1" if on else "0",))

    def today(self):
        return time.strftime("%Y-%m-%d")

    def used_today(self):
        r = self.w.one("SELECT value FROM meta WHERE key=?", ("arch:" + self.today(),))
        return int(r["value"]) if r else 0

    def status(self):
        if self.key_error:
            return f"key rejected: {self.key_error}"
        if not self.connected:
            return "no API key: build requests are not accepted"
        if self.current:
            return f"building request #{self.current}"
        if self.used_today() >= self.daily_limit:
            return f"limit reached ({self.used_today()}/{self.daily_limit} today), resumes at 00:00"
        return f"ready ({self.used_today()}/{self.daily_limit} used today)"

    def overview(self):
        reqs = self.w.q("SELECT * FROM requests ORDER BY id DESC LIMIT 40")
        for r in reqs:
            r["author_label"] = self.w.d(r["author"])
            r["code"] = (r.get("code") or "")[:6000]
        return {"status": self.status(), "connected": self.connected, "key_source": self.key_source(), "key_error": self.key_error,
                "auto_install": self.auto_install(), "daily_limit": self.daily_limit, "used_today": self.used_today(), "model": self.model,
                "current": self.current, "requests": reqs,
                "tools": self.w.q("SELECT name,description,args_doc,request_id,author FROM tools ORDER BY ts DESC")}

    # ------------------------------------------------------------ bookkeeping
    def recover(self):
        """A build interrupted by a shutdown goes back in the queue."""
        try:
            for r in self.w.q("SELECT id FROM requests WHERE status='building'"):
                self.w.x("UPDATE requests SET status='approved', note='interrupted by a restart; queued again' WHERE id=?", (r["id"],))
        except Exception:
            pass

    def note(self, req, text, status=None, **cols):
        sets, vals = ["note=?", "updated=?"], [text[:500], time.time()]
        if status:
            sets.append("status=?"); vals.append(status)
        for k, v in cols.items():
            sets.append(f"{k}=?"); vals.append(v)
        self.w.x(f"UPDATE requests SET {', '.join(sets)} WHERE id=?", (*vals, req["id"]))

    def no_key_sweep(self):
        """Approved build requests cannot be built without a key: tell their authors the truth instead of leaving them waiting."""
        if self.connected:
            return 0
        n = 0
        for r in self.w.q("SELECT * FROM requests WHERE status='approved' AND kind!='points'"):
            self.note(r, "no API key at the moment it was due to be built", status="no_key")
            if self.w.agent(r["author"]):
                self.w.send_mail("Architect", r["author"], f"About your request '{r['text'][:120]}': " + NO_KEY_TEXT.replace("so your request was not sent", "so it will not be built"))
            n += 1
        return n

    def tell_agents_limit(self):
        day = self.today()
        r = self.w.one("SELECT value FROM meta WHERE key='arch_notice'")
        if r and r["value"] == day:
            return
        self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES('arch_notice',?)", (day,))
        for r2 in self.w.q("SELECT DISTINCT author FROM requests WHERE status='approved'"):
            self.w.send_mail("Architect", r2["author"], "The architect has reached its build limit for today. Your approved request is queued and will be built after 00:00.")
        self.w.event("architect", "Architect", "Daily build limit reached. Approved requests stay queued until 00:00.", "arch")

    # ------------------------------------------------------------ model call
    def ask_once(self, system, user):
        if self.caller:
            return self.caller(system, user)
        key = self.key()
        if not key:
            raise KeyRejected("no key")
        body = json.dumps({"model": self.model, "max_tokens": 4000, "system": system,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read()).get("error", {}).get("message", "")
            except Exception:
                pass
            if e.code in (401, 403):
                raise KeyRejected(detail or f"HTTP {e.code}")
            if e.code in (408, 409, 429) or e.code >= 500:
                raise TransientError(f"HTTP {e.code} {detail}".strip())
            raise RuntimeError(f"HTTP {e.code} {detail}".strip())
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise TransientError(type(e).__name__)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def ask(self, system, user):
        last = None
        for i, wait in enumerate((0,) + RETRY_WAITS):
            if wait:
                self.w.event("architect", "Architect", f"API busy or unreachable ({last}); trying again in {wait} s.", "arch")
                time.sleep(wait)
            try:
                return self.ask_once(system, user)
            except TransientError as e:
                last = str(e)
        raise TransientError(last or "unreachable")

    @staticmethod
    def parse(raw):
        t = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
        try:
            v = json.loads(t)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            try:
                v = json.loads(t[a:b + 1])
                if isinstance(v, dict):
                    return v
            except Exception:
                pass
        return None

    # ------------------------------------------------------------ testing what Claude wrote
    def test_tool(self, spec):
        ex = spec.get("example_args") if isinstance(spec.get("example_args"), dict) else {}
        ok, val = sandbox.run(str(spec.get("code", "")), "run", [ex], timeout=4, helpers=False)
        if not ok:
            return False, str(val)[:600]
        if not isinstance(val, str):
            return False, f"run() returned {type(val).__name__}, it must return a string"
        ok2, val2 = sandbox.run(str(spec.get("code", "")), "run", [{}], timeout=4, helpers=False)
        if not ok2:
            return False, f"run({{}}) with no arguments crashed: {str(val2)[:400]}"
        return True, val

    def unique_name(self, name):
        name = re.sub(r"[^a-z0-9_]", "", str(name or "").lower())[:24] or "tool"
        if not re.match(r"^[a-z]", name):
            name = "t_" + name[:22]
        pl = getattr(self.w, "plugins", None)
        taken = {t["name"] for t in self.w.q("SELECT name FROM tools")} | set(pl.files() if pl else {}) | set(getattr(__import__("plugins"), "BUILTIN", set()))
        base, i = name, 2
        while name in taken or (pl and os.path.exists(os.path.join(pl.pending, name + ".py"))):
            name = f"{base[:21]}_{i}"
            i += 1
        return name

    # ------------------------------------------------------------ the pipeline
    def build_one(self):
        """Handle the oldest approved build request. Returns True if it did something."""
        req = self.w.one("SELECT * FROM requests WHERE status='approved' AND kind!='points' ORDER BY id LIMIT 1")
        if not req:
            return False
        if not self.connected:
            self.no_key_sweep()
            return True
        if self.used_today() >= self.daily_limit:
            self.tell_agents_limit()
            return False
        if self.w.one("SELECT COUNT(*) AS n FROM tools")["n"] >= MAX_TOOLS:
            return self.finish(req, "rejected", "The house already has as many tools as it can hold.")
        self.current = req["id"]
        try:
            return self._build(req)
        except KeyRejected as e:
            self.key_error = str(e)[:160] or "rejected"
            self.note(req, f"the API key was rejected ({self.key_error}); waiting for a working key", status="approved")
            self.w.event("architect", "Architect", f"The API key was rejected ({self.key_error}). Enter a working key in the Architect tab.", "arch")
            return False
        except TransientError as e:
            self.note(req, f"the API could not be reached after several tries ({e}); will try again in 5 minutes", status="approved")
            self._wake = time.time() + 300
            return False
        finally:
            self.current = None

    def _build(self, req):
        self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("arch:" + self.today(), str(self.used_today() + 1)))
        self.note(req, "Claude is writing the code", status="building", attempts=(req.get("attempts") or 0) + 1)
        have = ", ".join(t["name"] for t in self.w.q("SELECT name FROM tools")) or "none"
        plugs = ", ".join(sorted(self.w.plugins.files())) if getattr(self.w, "plugins", None) else "none"
        import plugins as _pm
        system = SYSTEM.replace("@BUILTIN@", ", ".join(sorted(_pm.BUILTIN)) or "none")
        self.w.event("architect", "Architect", f"Working on request #{req['id']} from {self.w.d(req['author'])}: {req['text'][:200]}", "arch")
        user = f"Request from an agent: {req['text']}\nExisting tools: {have}\nExisting plugins: {plugs}"
        spec = self.parse(self.ask(system, user))
        if spec is None:
            spec = self.parse(self.ask(system, user + "\n\nYour last reply was not valid JSON. Reply with ONLY the JSON object."))
        if spec is None:
            return self.finish(req, "failed", "The architect's answer could not be read.")
        if not spec.get("feasible"):
            return self.finish(req, "rejected", "The architect says this cannot be built as code: " + str(spec.get("reason", ""))[:240])
        kind = "plugin" if str(spec.get("kind", "tool")).lower() == "plugin" else "tool"
        name = self.unique_name(spec.get("name"))
        for attempt in (1, 2):
            code = str(spec.get("code", ""))
            ok, err = (self.test_tool(spec) if kind == "tool" else self.load_plugin(name, code, dry=True))
            if ok:
                break
            if attempt == 2:
                self.note(req, f"failed its test twice: {err[:300]}", code=code)
                return self.finish(req, "failed", f"The {kind} the architect wrote failed its test, even after a fix ({err[:200]}).")
            self.note(req, f"failed its test, asking Claude for a fix: {err[:200]}", code=code)
            self.w.event("architect", "Architect", f"Request #{req['id']}: the {kind} failed its test ({err[:160]}). Asking for a fix.", "arch")
            fixed = self.parse(self.ask(system, user + f"\n\nYou wrote this {kind}:\n{code}\n\nIt failed when the house tested it:\n{err}\n\nReturn the whole corrected JSON object."))
            if not fixed or not fixed.get("code"):
                return self.finish(req, "failed", f"The {kind} failed its test and the fix could not be read.")
            spec.update({k: v for k, v in fixed.items() if v not in (None, "")})
        code = str(spec.get("code", ""))
        desc = str(spec.get("description", ""))[:200]
        if kind == "tool":
            self.w.x("INSERT INTO tools(name,description,args_doc,code,ts,request_id,author) VALUES(?,?,?,?,?,?,?)",
                     (name, desc, str(spec.get("args", "{}"))[:200], code, time.time(), req["id"], req["author"]))
            self.note(req, f"tool '{name}' installed", code=code, result=name)
            return self.finish(req, "built", f"Your request was built as a tool called {name}: {desc} Use it with use_tool, "
                                             f'for example {{"action": "use_tool", "args": {{"tool": "{name}", "args": {json.dumps(spec.get("example_args") or {})}}}}}.', name)
        head = f"# built by: the architect for request #{req['id']} ({self.w.d(req['author'])})\n# about: {desc}\n"
        if self.auto_install():
            ok, msg = self.w.plugins.add(name, head + code)
            if ok:
                self.note(req, f"plugin '{name}' installed and running", code=code, result=name)
                acts = ", ".join((self.w.plugins.mods.get(name) or {}).get("actions") or [])
                return self.finish(req, "built", f"Your request was built as a plugin called {name} and is live now: {desc}" + (f" New actions: {acts}." if acts else ""), "plugin " + name)
            self.w.plugins.delete(name)
            self.w.plugins.add(name, head + code, pending=True)
            self.note(req, f"plugin did not start ({msg}); saved for the homeowner to look at", code=code, result=name)
            return self.finish(req, "failed", f"The plugin was written but did not start ({str(msg)[:160]}). The homeowner can look at it in Code & tools.")
        self.w.plugins.add(name, head + code, pending=True)
        self.note(req, f"plugin '{name}' written; waiting for the homeowner to install it (auto-install is off)", code=code, result=name)
        return self.finish(req, "built", f"Your request was written as a plugin called {name}: {desc} It becomes available once the homeowner installs it.", "plugin " + name)

    def load_plugin(self, name, code, dry=True):
        """Check a plugin without leaving it installed: it must parse, import, and expose sane ACTIONS."""
        import ast
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"syntax error: {e.msg} (line {e.lineno})"
        bad = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                bad |= {x.name.split(".")[0] for x in n.names} & {"os", "subprocess", "socket", "shutil", "urllib", "http", "requests", "ctypes", "sys", "pathlib"}
            elif isinstance(n, ast.ImportFrom) and n.module:
                bad |= {n.module.split(".")[0]} & {"os", "subprocess", "socket", "shutil", "urllib", "http", "requests", "ctypes", "sys", "pathlib"}
            elif isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("eval", "exec", "open", "__import__", "compile"):
                bad.add(n.func.id)
        if bad:
            return False, f"uses things plugins must not use: {', '.join(sorted(bad))}"
        ns = {}
        try:
            exec(compile(code, f"<architect:{name}>", "exec"), ns)
        except Exception as e:
            return False, f"{type(e).__name__} when loading: {e}"
        acts = ns.get("ACTIONS", {})
        if acts and not isinstance(acts, dict):
            return False, "ACTIONS must be a dict"
        import plugins as _pm
        mods = getattr(getattr(self.w, "plugins", None), "mods", {}) or {}
        for k, v in (acts or {}).items():
            k = str(k).lower()
            if not _pm.SAFE_ACTION.match(k):
                return False, f"action name '{k}' must be 2-24 lowercase letters, digits or underscores"
            if k in _pm.BUILTIN:
                return False, f"action '{k}' clashes with a built-in action"
            if any(k in (m.get("actions") or {}) for m in mods.values()):
                return False, f"action '{k}' already exists in another plugin"
            if not isinstance(v, dict) or not callable(v.get("run")) or not str(v.get("doc", "")).strip():
                return False, f"action '{k}' needs a 'doc' string and a 'run' function"
            try:
                out = v["run"](_DryCtx(), "a0", {})
                if not isinstance(out, str):
                    return False, f"action '{k}' returned {type(out).__name__}; it must return a string"
            except Exception as e:
                return False, f"action '{k}' crashed when called with no arguments: {type(e).__name__}: {e}"
        if not acts and not any(callable(ns.get(f)) for f in ("on_event", "on_tick")):
            return False, "the plugin defines no ACTIONS, on_event or on_tick, so it would do nothing"
        return True, ""

    def finish(self, req, status, message, tool=""):
        self.w.x("UPDATE requests SET status=?, note=?, updated=? WHERE id=?", (status, message[:500], time.time(), req["id"]))
        self.w.send_mail("Architect", req["author"], message)
        self.w.event("architect", "Architect", f"Request #{req['id']} {status}" + (f": {tool}" if tool else f": {message[:200]}"), "arch")
        if status == "built":
            for a in self.w.q("SELECT name FROM agents WHERE status='house' AND name!=?", (req["author"],)):
                self.w.mem(a["name"], "event", f"A new {tool.split(' ')[0] if tool.startswith('plugin') else 'tool'} exists in the house: {tool.split(' ')[-1]}.", 4)
        return True

    def run_forever(self):
        while True:
            try:
                if time.time() < self._wake or not self.build_one():
                    time.sleep(3)
            except Exception as e:
                self.w.event("sys", "House", f"architect error: {e!r}", "sys")
                time.sleep(10)
