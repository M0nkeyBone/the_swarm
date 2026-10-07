"""Hot-loaded plugins for the House. Add, change or remove a plugin while the House runs; nothing restarts.

A plugin is one Python file in the plugins/ folder (or pasted into the Plugins panel in the viewer, which
writes the file for you). The folder is watched every 2 seconds:
  new file      -> loaded, agents are told about any new actions
  changed file  -> reloaded
  deleted file  -> unloaded
  name.py.off   -> disabled (rename a file to .off to turn it off, or use the viewer's switch)

Plugins run inside the House process with full Python access. Only install code you trust.
Code written by the architect is saved to plugins/pending/ and does nothing until you click Install.

A plugin file may define any of these (all optional except it must be valid Python):
    DESCRIPTION = "one line for you"
    ACTIONS = {"roll_dice": {"doc": '{"sides": 6}  roll a die', "run": roll}}
        run(ctx, agent, args) -> str       agent = agent id string; the string is what the agent sees
    def on_event(ctx, ev): ...             every logged event: dict(channel, actor, text, type)
    def on_tick(ctx): ...                  about every 5 seconds
    def setup(ctx): ... / def teardown(ctx): ...
    VIEWER_JS = "..."                      javascript added to the viewer page (see viewer docs)
ctx offers: ctx.name (plugin name), ctx.store (dict that persists across restarts, JSON values only),
ctx.agents() (list of dicts: id, display, room), ctx.display(agent), ctx.say(agent, text),
ctx.remember(agent, text, importance=3), ctx.mail(agent, text), ctx.event(text), ctx.board(text), ctx.log(text)
"""
import ast
import importlib.util
import json
import os
import re
import threading
import time
import traceback

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
SAFE_ACTION = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
BUILTIN = set()      # filled by world: action names plugins may not replace


class Ctx:
    def __init__(self, mgr, name):
        self._m, self.name = mgr, name
        raw = mgr.world.one("SELECT value FROM meta WHERE key=?", ("plugin:" + name,))
        try:
            self.store = json.loads(raw["value"]) if raw else {}
        except Exception:
            self.store = {}

    def save(self):
        self._m.world.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("plugin:" + self.name, json.dumps(self.store)[:20000]))

    def agents(self):
        w = self._m.world
        return [{"id": a["name"], "display": w.d(a["name"]), "room": a["room"]} for a in w.q("SELECT * FROM agents WHERE status='house'")]

    def display(self, agent):
        return self._m.world.d(agent)

    def say(self, agent, text):
        self._m.world.say(agent, str(text)[:80])

    def remember(self, agent, text, importance=3):
        self._m.world.mem(agent, "event", str(text)[:300], int(importance))

    def mail(self, agent, text):
        self._m.world.send_mail("House", agent, str(text)[:500])

    def board(self, text):
        w = self._m.world
        w.x("INSERT INTO board(ts,author,text) VALUES(?,?,?)", (time.time(), "House", str(text)[:500]))
        w.event("board", "House", f"House: {str(text)[:500]}")

    def event(self, text):
        self._m.world.event(f"plugin: {self.name}", "House", str(text)[:300], "plugin")

    def log(self, text):
        self.event(text)


class Manager:
    def __init__(self, world, folder):
        self.world = world
        self.dir = folder
        self.pending = os.path.join(folder, "pending")
        os.makedirs(self.pending, exist_ok=True)
        self.mods = {}          # name -> dict(mod, ctx, mtime, actions)
        self.errors = {}        # name -> message
        self.lock = threading.RLock()
        self._tick = 0
        self._in_hook = False

    # ------------------------------------------------------------ files
    def files(self):
        out = {}
        for f in os.listdir(self.dir):
            p = os.path.join(self.dir, f)
            if os.path.isfile(p) and (f.endswith(".py") or f.endswith(".py.off")) and not f.startswith("_"):
                out[f.split(".")[0]] = (p, f.endswith(".off"))
        return out

    def scan(self):
        """Load, reload and unload to match the folder. Safe to call often."""
        changed = []
        with self.lock:
            files = self.files()
            for name in list(self.mods):
                if name not in files or files[name][1]:
                    self._unload(name)
                    changed.append(("unloaded", name))
            for name, (path, off) in files.items():
                if off:
                    self.errors.pop(name, None)
                    continue
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                cur = self.mods.get(name)
                if cur and cur["mtime"] == mt:
                    continue
                if not NAME_RE.match(name):
                    self.errors[name] = "file name must be lowercase letters, digits and underscores (2 to 24 characters)"
                    continue
                was = bool(cur)
                if cur:
                    self._unload(name)
                if self._load(name, path, mt):
                    changed.append(("reloaded" if was else "loaded", name))
                else:
                    changed.append(("error", name))
        for kind, name in changed:
            self._announce(kind, name)
        return changed

    def _unload(self, name):
        m = self.mods.pop(name, None)
        if m:
            try:
                if hasattr(m["mod"], "teardown"):
                    m["mod"].teardown(m["ctx"])
            except Exception:
                pass

    def _load(self, name, path, mtime):
        try:
            src = open(path, encoding="utf-8").read()
            ast.parse(src)
            spec = importlib.util.spec_from_file_location("house_plugin_" + name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            ctx = Ctx(self, name)
            acts = {}
            for k, v in (getattr(mod, "ACTIONS", {}) or {}).items():
                k = str(k).lower()
                if not SAFE_ACTION.match(k) or k in BUILTIN:
                    raise ValueError(f"action name not allowed: {k}")
                if not callable(v.get("run")) or not str(v.get("doc", "")).strip():
                    raise ValueError(f"action {k} needs a 'doc' string and a callable 'run'")
                for other, om in self.mods.items():
                    if k in om["actions"]:
                        raise ValueError(f"action {k} already exists in plugin {other}")
                acts[k] = {"doc": str(v["doc"])[:600], "run": v["run"]}
            if hasattr(mod, "setup"):
                mod.setup(ctx)
            self.mods[name] = {"mod": mod, "ctx": ctx, "mtime": mtime, "actions": acts, "src": src}
            self.errors.pop(name, None)
            return True
        except BaseException as e:
            self.errors[name] = f"{type(e).__name__}: {str(e)[:200]}"
            self.mods.pop(name, None)
            return False

    def _announce(self, kind, name):
        w = self.world
        if kind == "error":
            w.event("plugin: " + name, "House", f"Plugin {name} could not load: {self.errors.get(name)}", "plugin")
            return
        if kind == "unloaded":
            w.event("plugin: " + name, "House", f"Plugin {name} was removed or turned off.", "plugin")
            return
        acts = self.mods.get(name, {}).get("actions", {})
        w.event("plugin: " + name, "House", f"Plugin {name} {kind}" + (f" with actions: {', '.join(acts)}" if acts else "") + ". No restart needed.", "plugin")
        if kind == "loaded" and acts:
            for a in w.q("SELECT name FROM agents WHERE status='house'"):
                w.mem(a["name"], "event", "You can now also do these things in the house: " + "; ".join(f"{k} {v['doc'][:160]}" for k, v in acts.items()), 6)

    # ------------------------------------------------------------ used by the world
    def actions(self):
        with self.lock:
            return {k: v for m in self.mods.values() for k, v in m["actions"].items()}

    def run_action(self, action, agent, args):
        with self.lock:
            fn = None
            for m in self.mods.values():
                if action in m["actions"]:
                    fn, ctx = m["actions"][action]["run"], m["ctx"]
        if not fn:
            return "That is not possible here."
        box = {}

        def go():
            try:
                box["r"] = fn(ctx, agent, args)
            except BaseException as e:
                box["e"] = f"{type(e).__name__}: {str(e)[:150]}"
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(5)
        if t.is_alive():
            return "It took too long and did not finish."
        if "e" in box:
            self.world.event(f"plugin: {action}", "House", f"Plugin action {action} failed: {box['e']}", "plugin")
            return "It did not work."
        try:
            ctx.save()
        except Exception:
            pass
        return str(box.get("r", ""))[:800] or "Done."

    def on_event(self, ev):
        if self._in_hook:
            return
        self._in_hook = True
        try:
            for name, m in list(self.mods.items()):
                f = getattr(m["mod"], "on_event", None)
                if f:
                    try:
                        f(m["ctx"], ev)
                        m["ctx"].save()
                    except Exception as e:
                        self.errors[name] = "on_event: " + str(e)[:150]
        finally:
            self._in_hook = False

    def tick(self):
        now = time.time()
        if now - self._tick < 5:
            return
        self._tick = now
        for name, m in list(self.mods.items()):
            f = getattr(m["mod"], "on_tick", None)
            if f:
                try:
                    f(m["ctx"])
                    m["ctx"].save()
                except Exception as e:
                    self.errors[name] = "on_tick: " + str(e)[:150]

    def viewer_js(self):
        with self.lock:
            return {n: getattr(m["mod"], "VIEWER_JS", "") for n, m in self.mods.items() if getattr(m["mod"], "VIEWER_JS", "")}

    # ------------------------------------------------------------ homeowner / architect operations
    def listing(self):
        files = self.files()
        out = []
        for name, (path, off) in sorted(files.items()):
            m = self.mods.get(name)
            out.append({"name": name, "enabled": not off, "loaded": bool(m), "error": self.errors.get(name, ""),
                        "description": str(getattr(m["mod"], "DESCRIPTION", ""))[:160] if m else "",
                        "actions": sorted(m["actions"]) if m else [], "js": bool(m and getattr(m["mod"], "VIEWER_JS", "")),
                        "code": (m["src"] if m else open(path, encoding="utf-8", errors="replace").read())[:6000]})
        pend = []
        for f in sorted(os.listdir(self.pending)):
            if f.endswith(".py"):
                src = open(os.path.join(self.pending, f), encoding="utf-8", errors="replace").read()[:8000]
                mb = re.search(r"# submitted by: \S+ \(([^)]*)\)", src)
                fl = re.search(r"# flags: (.*)", src)
                pend.append({"name": f[:-3], "by": mb.group(1) if mb else "the architect", "flags": (fl.group(1).strip() if fl and fl.group(1).strip() != "none" else ""), "code": open(os.path.join(self.pending, f), encoding="utf-8", errors="replace").read()[:8000]})
        return {"plugins": out, "pending": pend, "folder": os.path.abspath(self.dir)}

    def add(self, name, code, pending=False):
        name = re.sub(r"[^a-z0-9_]", "", str(name).lower().replace("-", "_").replace(" ", "_"))[:24]
        if not NAME_RE.match(name):
            return False, "name: 2 to 24 characters, lowercase letters, digits, underscores, starting with a letter"
        try:
            ast.parse(code)
        except SyntaxError as e:
            return False, f"syntax error: {e.msg} (line {e.lineno})"
        if len(code) > 60000:
            return False, "too long"
        folder = self.pending if pending else self.dir
        path = os.path.join(folder, name + ".py")
        if not pending:
            for ext in (".py.off",):
                p2 = os.path.join(self.dir, name + ext)
                if os.path.exists(p2):
                    os.remove(p2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        if not pending:
            self.scan()
            if name in self.errors:
                return False, self.errors[name]
        return True, name

    def approve(self, name):
        p = os.path.join(self.pending, name + ".py")
        if not os.path.exists(p):
            return False, "not found"
        code = open(p, encoding="utf-8").read()
        ok, msg = self.add(name, code)
        if ok:
            os.remove(p)
        return ok, msg

    def reject(self, name):
        p = os.path.join(self.pending, re.sub(r"[^a-z0-9_]", "", name) + ".py")
        if os.path.exists(p):
            os.remove(p)
            return True
        return False

    def toggle(self, name, enabled):
        name = re.sub(r"[^a-z0-9_]", "", name)
        a, b = os.path.join(self.dir, name + ".py"), os.path.join(self.dir, name + ".py.off")
        if enabled and os.path.exists(b):
            os.replace(b, a)
        elif not enabled and os.path.exists(a):
            os.replace(a, b)
        else:
            return False
        self.scan()
        return True

    def delete(self, name):
        name = re.sub(r"[^a-z0-9_]", "", name)
        hit = False
        for ext in (".py", ".py.off"):
            p = os.path.join(self.dir, name + ext)
            if os.path.exists(p):
                os.remove(p)
                hit = True
        self.scan()
        return hit

    def run_forever(self):
        while True:
            try:
                self.scan()
            except Exception:
                traceback.print_exc()
            time.sleep(2)
