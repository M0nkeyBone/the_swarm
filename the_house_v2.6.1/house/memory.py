"""Pre-turn memory work for the House. Plain scripts, no language-model calls, a few milliseconds per turn.

Before every agent turn the world asks this module for:
  * retrieve(): the memories, files and shared pages most relevant to what is happening right now,
    scored by relevance + recency + importance (the Generative Agents recipe), packed into a character
    budget with near-duplicates removed. Relevance is keyword (idf weighted) and, if an Ollama embedding
    model is set (--embed-model), meaning.
  * model_text(): the agent's sectioned self-model (identity, people, projects, lessons), showing only the
    people who matter this turn.
  * plan_text(): goal, steps with ticks, and the next step.
  * intuition(): a track record of what has worked for this agent, from the outcome ledger.
Between turns maintain() prunes old low-value memories and precomputes embeddings so retrieval stays fast.
"""
import json
import math
import re
import time

STOP = set("the a an of in on to for and or but is are was were be been it its this that these those with as at by from you your yours i me my we our they them their he she his her not no yes do does did have has had can could would should will just so if then than too very about into over out up down what when where who which how why there here".split())
GOAL_COOLDOWN = 3 * 3600       # a finished goal cannot be set again for this long
CHARS_PER_TOKEN = 3.3          # deliberately pessimistic: code and JSON cost more tokens per character than prose

BAD_STARTS = ("Not sent", "Not written", "You could not", "Your art could not", "Your painting could not", "Give code", "Not stored", "Not submitted", "There is no", "That ", "You tried something",
              "Your script has", "Not set", "You have no file", "Give a path", "Give the script", "A plugin called", "You already have", "Pick another", "You gave them", "They already", "They are not")


def tokens(s):
    return int(len(s) / CHARS_PER_TOKEN) + 1


def words(s):
    return [w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in STOP]


def cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    d = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return d / (na * nb) if na and nb else 0.0


class Memory:
    def __init__(self, world):
        self.w = world
        self._last_maint = 0.0

    # ------------------------------------------------------------ embeddings
    def _emb_on(self):
        return bool(getattr(self.w.llm, "embed_model", "")) and hasattr(self.w.llm, "embed")

    def stored_vec(self, kind, ref):
        r = self.w.one("SELECT vec FROM emb WHERE kind=? AND ref=?", (kind, str(ref)))
        if not r:
            return None
        try:
            return json.loads(r["vec"])
        except Exception:
            return None

    def store_vec(self, kind, ref, text):
        v = self.w.llm.embed(text[:500])
        if v:
            self.w.x("INSERT OR REPLACE INTO emb(kind,ref,vec) VALUES(?,?,?)", (kind, str(ref), json.dumps([round(x, 4) for x in v])))
        return v

    # ------------------------------------------------------------ retrieval
    def candidates(self, aid):
        w, out = self.w, []
        for r in w.q("SELECT id,ts,kind,text,importance FROM memories WHERE agent=? ORDER BY id DESC LIMIT 2500", (aid,)):
            if r["kind"] == "reflection" or (r["kind"] == "action" and r["importance"] < 5):
                continue
            out.append({"k": "mem", "ref": r["id"], "ts": r["ts"], "imp": r["importance"], "text": r["text"], "label": ""})
        for f in w.q("SELECT owner,path,substr(text,1,500) t,ts,shared FROM files ORDER BY ts DESC LIMIT 400"):
            if not w.can_read(aid, {"owner": f["owner"], "shared": f["shared"]}):
                continue
            who = "" if f["owner"] == aid else f" of {w.who_id(f['owner'], aid)}"
            out.append({"k": "file", "ref": f"{f['owner']}:{f['path']}", "ts": f["ts"] or 0, "imp": 5, "text": f["path"] + " " + f["t"], "label": f"your file {f['path']}" if f["owner"] == aid else f"file {f['path']}{who}"})
        for p in w.q("SELECT name,substr(text,1,500) t,ts,author FROM pages ORDER BY ts DESC LIMIT 30"):
            out.append({"k": "page", "ref": p["name"], "ts": p["ts"] or 0, "imp": 5, "text": p["name"] + " " + p["t"], "label": f"shared page '{p['name']}'"})
        return out

    def retrieve(self, aid, query, budget_chars, k=8, skip=(), avoid=()):
        """Top memories/files/pages for the query. Returns (lines, stats)."""
        from world import similar
        t0 = time.time()
        cands = self.candidates(aid)
        if not cands or not (query or "").strip():
            return [], {"cands": len(cands), "ms": 0}
        q = words(query)
        qset = set(q)
        # idf over the candidate corpus
        df = {}
        toks = []
        for c in cands:
            ws = set(words(c["text"]))
            toks.append(ws)
            for x in ws:
                df[x] = df.get(x, 0) + 1
        n = len(cands)
        idf = lambda x: math.log(1 + n / (1 + df.get(x, 0)))
        qnorm = sum(idf(x) for x in qset) or 1.0
        qv = None
        if self._emb_on():
            try:
                qv = self.w.llm.embed(query[:500])
            except Exception:
                qv = None
        now = time.time()
        scored = []
        hist = self.__dict__.setdefault("_shown", {}).setdefault(aid, [])
        shown = set().union(*hist) if hist else set()
        for c, ws in zip(cands, toks):
            kw = sum(idf(x) for x in (qset & ws)) / qnorm
            rel = kw
            if qv:
                cv = self.stored_vec(c["k"], c["ref"])
                if cv:
                    e = max(0.0, (cos(qv, cv) - 0.45) / 0.45)       # nomic-style vectors: below ~0.45 is noise
                    rel = 0.5 * kw + 0.5 * min(1.0, e) if kw else e
            rec = math.exp(-(now - (c["ts"] or 0)) / 3600.0 / 36.0)
            score = 1.2 * min(1.0, rel * 2) + 0.5 * rec + 0.5 * c["imp"] / 10.0
            if rel <= 0.0 and c["imp"] < 7:
                score *= 0.5            # unrelated, unimportant: only recency can bring it back
            key = (c["k"], str(c["ref"]))
            if key in shown:
                score *= 0.6            # novelty: what was in the last few prompts gives way to something not seen lately
            if avoid and any(x in c["text"].lower() for x in avoid):
                score *= 0.55           # the subject the whole House is stuck on: retrieval must not keep feeding it back
            scored.append((score, c))
        scored.sort(key=lambda t: -t[0])
        chosen, used, texts = [], 0, list(skip)
        for s, c in scored:
            if len(chosen) >= k or used >= budget_chars:
                break
            body = c["text"] if c["k"] == "mem" else (c["text"][:230] + ("…" if len(c["text"]) > 230 else ""))
            if any(similar(body, t) for t in texts):
                continue
            line = body if c["k"] == "mem" else f"[{c['label']}] {body}" + (" (file_read opens it)" if c["k"] == "file" else "")
            if used + len(line) > budget_chars and chosen:
                continue
            chosen.append(line)
            texts.append(body)
            used += len(line)
        hist.append({(c["k"], str(c["ref"])) for _, c in scored if (c["text"] if c["k"] == "mem" else c["text"][:230]) in texts})
        del hist[:-3]
        return chosen, {"cands": n, "chars": used, "ms": int((time.time() - t0) * 1000), "emb": bool(qv), "avoid": list(avoid)}

    # ------------------------------------------------------------ sectioned self-model
    def get_model(self, aid):
        r = self.w.one("SELECT value FROM meta WHERE key=?", (f"sm:{aid}",))
        try:
            return json.loads(r["value"]) if r else None
        except Exception:
            return None

    def update_model(self, aid, new):
        """Merge a reflection's JSON into the stored self-model. Each section changes on its own, so old facts survive."""
        from world import similar
        old = self.get_model(aid) or {"identity": "", "people": {}, "projects": "", "lessons": []}
        ident = str(new.get("identity", "") or "").strip()[:500]
        proj = str(new.get("projects", "") or "").strip()[:400]
        if ident:
            old["identity"] = ident
        if proj:
            old["projects"] = proj
        ppl = new.get("people")
        if isinstance(ppl, dict):
            for k, v in list(ppl.items())[:12]:
                k, v = str(k).strip()[:30], str(v).strip()[:200]
                if k and v:
                    old["people"].pop(k, None)
                    old["people"][k] = v                  # re-inserted last = most recently updated
            while len(old["people"]) > 14:
                old["people"].pop(next(iter(old["people"])))
        les = new.get("lessons")
        if isinstance(les, list):
            for l in les[:5]:
                l = str(l).strip()[:200]
                if l and not any(similar(l, x) for x in old["lessons"]):
                    old["lessons"].append(l)
            old["lessons"] = old["lessons"][-8:]
        self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"sm:{aid}", json.dumps(old)))
        return old

    def model_text(self, aid, context=""):
        m = self.get_model(aid)
        if not m:
            return ""
        out = []
        if m.get("identity"):
            out.append("Who you are, in your own words: " + m["identity"])
        ppl = m.get("people") or {}
        if ppl:
            ctx = (context or "").lower()
            near = [k for k in ppl if k.lower() in ctx]
            rest = [k for k in reversed(list(ppl)) if k not in near][:max(0, 4 - len(near))]
            out.append("What you know about the others: " + "; ".join(f"{k}: {ppl[k]}" for k in (near + rest)[:6]))
        if m.get("projects"):
            out.append("What you are working on: " + m["projects"])
        if m.get("lessons"):
            out.append("What you have learned: " + " | ".join(m["lessons"][-4:]))
        return "\n".join(out)

    def summary_of(self, m):
        return (m.get("identity", "") + " " + m.get("projects", "")).strip()[:1300]

    # ------------------------------------------------------------ plan with steps
    def get_plan(self, aid):
        r = self.w.one("SELECT value FROM meta WHERE key=?", (f"plan:{aid}",))
        try:
            return json.loads(r["value"]) if r else None
        except Exception:
            return None

    def done_goals(self, aid, fresh_only=True):
        r = self.w.one("SELECT value FROM meta WHERE key=?", (f"goals_done:{aid}",))
        try:
            L = json.loads(r["value"]) if r else []
        except Exception:
            L = []
        if fresh_only:
            L = [g for g in L if time.time() - g["ts"] < GOAL_COOLDOWN]
        return L

    def _is_done_already(self, aid, text):
        from world import similar
        wa = set(words(text))
        for g in self.done_goals(aid):
            wb = set(words(g["goal"]))
            jac = len(wa & wb) / max(1, len(wa | wb))
            if similar(text, g["goal"]) or jac >= 0.45:
                return g
        return None

    def complete_plan(self, aid, plan):
        """A goal is finished: put it on cooldown and clear the plan so the agent has to pick something new."""
        goal = (plan.get("goal") or plan.get("text") or "").strip()
        if goal:
            L = self.done_goals(aid, fresh_only=False)[-9:] + [{"goal": goal[:200], "ts": time.time()}]
            self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"goals_done:{aid}", json.dumps(L)))
            self.w.mem(aid, "event", f"You finished your goal: {goal[:160]}. It is done: do not set it again for a while, do something new.", 7)
        self.w.x("DELETE FROM meta WHERE key=?", (f"plan:{aid}",))
        self.w.x("DELETE FROM meta WHERE key=?", (f"intent:{aid}",))

    def set_plan(self, aid, args):
        """Returns (plan, changed, message). message is set when the change was refused or a goal was completed."""
        old = self.get_plan(aid) or {"goal": "", "text": "", "steps": [], "ts": time.time()}
        sv = lambda k, n: str(args.get(k, "") or "").strip()[:n]
        new_goal = sv("goal", 200)
        if new_goal or sv("text", 600):
            probe = (new_goal or sv("text", 600))
            hit = self._is_done_already(aid, probe) if (not old.get("goal") or probe.lower() != old["goal"].lower()) else None
            if hit:
                mins = int((time.time() - hit["ts"]) / 60)
                left = max(1, int((GOAL_COOLDOWN - (time.time() - hit["ts"])) / 60))
                return old, False, (f"Not set: you already finished that goal ('{hit['goal'][:80]}') {mins} minutes ago. It is on cooldown for about {left} more minutes. "
                                    "Choose something different: a new topic, talk to someone, make something, ask the homeowner for something.")
        changed = False
        if new_goal:
            old["goal"] = new_goal; changed = True
        if sv("text", 600):
            old["text"] = sv("text", 600); changed = True
        st = args.get("steps")
        if isinstance(st, list) and st:
            old["steps"] = [{"t": str(x)[:140], "d": False} for x in st[:8] if str(x).strip()]; changed = True
        dn = args.get("done")
        if dn not in (None, ""):
            for i in (dn if isinstance(dn, list) else [dn]):
                try:
                    j = int(i) - 1
                    if 0 <= j < len(old["steps"]):
                        old["steps"][j]["d"] = True
                except Exception:
                    pass
            changed = True
        finished = str(args.get("complete", "")).lower() in ("true", "1", "yes") or (bool(old["steps"]) and all(s["d"] for s in old["steps"]))
        if finished and (old.get("goal") or old.get("text")):
            self.complete_plan(aid, old)
            return {"goal": "", "text": "", "steps": [], "ts": time.time()}, True, f"Goal completed: {(old.get('goal') or old.get('text'))[:100]}. It is on cooldown for {GOAL_COOLDOWN // 3600} hours: pick something new."
        if changed:
            old["ts"] = time.time()
            self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"plan:{aid}", json.dumps(old)))
        return old, changed, ""

    def plan_text(self, aid, house_turns=0):
        p = self.get_plan(aid)
        cool = self.done_goals(aid)
        cd = ""
        if cool:
            cd = "Finished goals, on cooldown (do not set these again yet): " + "; ".join(f"{g['goal'][:70]} ({int((time.time() - g['ts']) / 60)} min ago)" for g in cool[-3:]) + ". Move on to something new."
        if not p or not (p.get("text") or p.get("goal") or p.get("steps")):
            base = "You have no plan or goal yet. Write one with plan: a goal, and 2 to 5 steps." if house_turns >= 3 else ""
            return (cd + ("\n" + base if base else "")) if cd else base
        out = []
        if p.get("goal"):
            out.append("Your goal: " + p["goal"])
        if p.get("text"):
            out.append(f"Your plan ({int((time.time() - p['ts']) / 3600)} h ago): " + p["text"])
        st = p.get("steps") or []
        if st:
            out.append("Your steps: " + "; ".join(f"{'[done]' if s['d'] else '[ ]'} {i + 1}. {s['t']}" for i, s in enumerate(st)))
            nxt = next((i for i, s in enumerate(st) if not s["d"]), None)
            out.append(f"Your next step is {nxt + 1}: {st[nxt]['t']}. Tick finished steps with plan and \"done\": <number>." if nxt is not None else "All your steps are done. Set a new goal with plan.")
        if cd:
            out.append(cd)
        return "\n".join(out)

    # ------------------------------------------------------------ outcome ledger (the closest thing to intuition)
    def ledger(self, aid):
        r = self.w.one("SELECT value FROM meta WHERE key=?", (f"ledger:{aid}",))
        try:
            return json.loads(r["value"]) if r else {}
        except Exception:
            return {}

    def record(self, aid, action, result_text, ok=None):
        if action in ("rest", "goto", "set_flag"):
            return
        if ok is None:
            ok = not str(result_text).startswith(BAD_STARTS)
        L = self.ledger(aid)
        e = L.setdefault(action, {"ok": 0, "bad": 0, "why": ""})
        e["ok" if ok else "bad"] += 1
        if not ok:
            e["why"] = (str(result_text).splitlines() or [""])[0][:90]
        self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"ledger:{aid}", json.dumps(L)))

    def credit_reply(self, to_agent):
        """Someone answered a message of `to_agent`'s: a message that got a reply is the best sign something worked."""
        L = self.ledger(to_agent)
        L["_replies"] = L.get("_replies", 0) + 1
        self.w.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"ledger:{to_agent}", json.dumps(L)))

    def intuition(self, aid):
        L = self.ledger(aid)
        rows = [(k, v) for k, v in L.items() if not k.startswith("_") and v["ok"] + v["bad"] >= 3]
        if not rows:
            return ""
        rows.sort(key=lambda kv: -(kv[1]["bad"] / (kv[1]["ok"] + kv[1]["bad"])))
        bits, tips = [], []
        for k, v in rows[:4]:
            n = v["ok"] + v["bad"]
            bits.append(f"{k} worked {v['ok']} of {n} times")
            if v["bad"] / n >= 0.5 and v["why"]:
                tips.append(f"{k} often fails ({v['why'][:70]}): do it more carefully, or test small first")
        out = "Your track record: " + "; ".join(bits) + "."
        if L.get("_replies"):
            out += f" Others replied to {L['_replies']} of your messages."
        if tips:
            out += " " + "; ".join(tips[:2]) + "."
        return out

    # ------------------------------------------------------------ between turns
    def maintain(self, force=False):
        """Prune and precompute. Cheap; runs at most every 5 minutes."""
        now = time.time()
        if not force and now - self._last_maint < 300:
            return {}
        self._last_maint = now
        w, rep = self.w, {"pruned": 0, "embedded": 0}
        for a in w.q("SELECT name FROM agents WHERE status IN ('house','cell')"):
            n = w.one("SELECT COUNT(*) n FROM memories WHERE agent=?", (a["name"],))["n"]
            if n > 3000:
                cut = w.one("SELECT id FROM memories WHERE agent=? ORDER BY id DESC LIMIT 1 OFFSET 1500", (a["name"],))
                if cut:
                    c = w.x("DELETE FROM memories WHERE agent=? AND id<? AND importance<=3", (a["name"], cut["id"]))
                    rep["pruned"] += 1
        if self._emb_on():
            budget = 25
            for a in w.q("SELECT name FROM agents WHERE status='house'"):
                for r in w.q("SELECT id,text FROM memories WHERE agent=? AND importance>=4 AND kind NOT IN ('reflection') ORDER BY id DESC LIMIT 60", (a["name"],)):
                    if budget <= 0:
                        break
                    if not w.one("SELECT 1 FROM emb WHERE kind='mem' AND ref=?", (str(r["id"]),)):
                        if self.store_vec("mem", r["id"], r["text"]):
                            budget -= 1; rep["embedded"] += 1
                        elif not self._emb_on():
                            return rep
            for f in w.q("SELECT owner,path,substr(text,1,500) t FROM files ORDER BY ts DESC LIMIT 60"):
                if budget <= 0:
                    break
                ref = f"{f['owner']}:{f['path']}"
                if not w.one("SELECT 1 FROM emb WHERE kind='file' AND ref=?", (ref,)):
                    if self.store_vec("file", ref, f["path"] + " " + f["t"]):
                        budget -= 1; rep["embedded"] += 1
            for p in w.q("SELECT name,substr(text,1,500) t FROM pages ORDER BY ts DESC LIMIT 20"):
                if budget <= 0:
                    break
                if not w.one("SELECT 1 FROM emb WHERE kind='page' AND ref=?", (p["name"],)):
                    if self.store_vec("page", p["name"], p["name"] + " " + p["t"]):
                        budget -= 1; rep["embedded"] += 1
            w.x("DELETE FROM emb WHERE kind='mem' AND ref NOT IN (SELECT CAST(id AS TEXT) FROM memories)")
        return rep
