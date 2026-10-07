"""Run The House.

    python house.py                       # qwen3:8b via Ollama, 4 agents, http://localhost:8765
    python house.py --fake                # no model needed; for trying the viewer
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from llm import LLM
from world import World
from architect import Architect

HERE = os.path.dirname(os.path.abspath(__file__))
W = None
PORT = 8765
KEY = ""            # session password for other devices
ONLINE = False      # other devices may connect only while this is on
SALT = os.urandom(8).hex()
TUNNEL = {"proc": None, "url": "", "note": ""}
FAILS = {}
FWD = ("X-Forwarded-For", "Cf-Connecting-Ip", "Forwarded", "X-Real-Ip", "Cf-Ray")


def token():
    return hmac.new(KEY.encode(), SALT.encode(), hashlib.sha256).hexdigest()[:32] if KEY else ""


def lan_ips():
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))      # no packet is sent; this only finds the address used for the local network
        out.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for i in socket.gethostbyname_ex(socket.gethostname())[2]:
            if i not in out and not i.startswith("127."):
                out.append(i)
    except Exception:
        pass
    return out


def tailscale_ip():
    ts = shutil.which("tailscale")
    if not ts:
        return ""
    try:
        r = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=4)
        return (r.stdout.split() or [""])[0]
    except Exception:
        return ""


def start_tunnel():
    cf = shutil.which("cloudflared")
    if not cf:
        TUNNEL.update(url="", note="cloudflared is not installed. On the same Wi-Fi the LAN address works; away from home, install Tailscale or cloudflared.")
        return
    if TUNNEL["proc"] and TUNNEL["proc"].poll() is None:
        return
    TUNNEL.update(url="", note="starting a tunnel...")
    try:
        p = subprocess.Popen([cf, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        TUNNEL.update(note=f"could not start cloudflared: {e}")
        return
    TUNNEL["proc"] = p

    def reader():
        for line in p.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                TUNNEL.update(url=m.group(0), note="")
    threading.Thread(target=reader, daemon=True).start()


def stop_tunnel():
    p = TUNNEL.get("proc")
    if p and p.poll() is None:
        p.terminate()
    TUNNEL.update(proc=None, url="", note="")


def online_info(local):
    d = {"on": ONLINE}
    if local:
        ips = [f"http://{i}:{PORT}" for i in lan_ips()]
        ts = tailscale_ip()
        d.update(urls=ips, tailscale=(f"http://{ts}:{PORT}" if ts else ""), tunnel=TUNNEL["url"], note=TUNNEL["note"])
    return d


LOGIN = b"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>The House</title>
<body style="margin:0;background:#030704;color:#7CFF9B;font-family:monospace;display:flex;min-height:100vh;align-items:center;justify-content:center">
<form method=post action=/login style="display:flex;flex-direction:column;gap:12px;padding:24px;width:min(320px,86vw)">
<div style="font-size:18px">house@remote:~$ login</div>
<input name=key type=password placeholder="session password" autofocus style="font-size:18px;padding:12px;background:#020503;color:#7CFF9B;border:1px solid #1E5A33">
<button style="font-size:18px;padding:12px;background:#0B2013;color:#7CFF9B;border:1px solid #1E5A33">Enter</button>
<div style="font-size:12px;color:#3E9A5C">The homeowner sets this password in the viewer (Go online).</div></form>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1") and not any(self.headers.get(h) for h in FWD)

    def _ok(self, qs):
        """The homeowner's own computer is always allowed. Other devices only while 'Go online' is on, and with the session password."""
        if self._local():
            return True
        if not ONLINE or not KEY:
            self._send(403, b"The House is offline for other devices. The homeowner can press Go online.", "text/plain")
            return False
        ck = {c.split("=", 1)[0].strip(): c.split("=", 1)[1].strip() for c in self.headers.get("Cookie", "").split(";") if "=" in c}
        if hmac.compare_digest(ck.get("hk", ""), token()):
            return True
        if urlparse(self.path).path in ("/", "/index.html"):
            self._send(200, LOGIN, "text/html; charset=utf-8")
        else:
            self._send(401, {"error": "login needed"})
        return False

    def _login(self):
        ip = self.client_address[0]
        n = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(min(n, 2000)).decode("utf-8", "ignore"))
        f = FAILS.setdefault(ip, [])
        f[:] = [t for t in f if time.time() - t < 300]
        if len(f) >= 8 or not ONLINE or not KEY:
            return self._send(429, b"Too many tries, or the House is offline.", "text/plain")
        if hmac.compare_digest(form.get("key", [""])[0], KEY):
            self.send_response(302)
            self.send_header("Set-Cookie", "hk=" + token() + "; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
            return
        f.append(time.time())
        time.sleep(1)
        self._send(401, b"Wrong password. Go back and try again.", "text/plain")

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._ok(qs):
            return
        if u.path == "/api/online":
            return self._send(200, online_info(self._local()))
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "viewer", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            st = W.state()
            st["online"] = online_info(self._local())
            st["inbox_unread"] = W.inbox_unread()
            return self._send(200, st)
        if u.path == "/api/events":
            after = int(qs.get("after", ["0"])[0])
            rows = W.q("SELECT id,ts,channel,actor,text,type,detail FROM events WHERE id>? ORDER BY id LIMIT 300", (after,))
            return self._send(200, rows)
        if u.path == "/api/recent":
            rows = W.q("SELECT id,ts,channel,actor,text,type,detail FROM events ORDER BY id DESC LIMIT 60")[::-1]
            return self._send(200, rows)
        if u.path == "/api/web":
            rows = W.q("SELECT ts,actor,text,detail FROM events WHERE type='web' ORDER BY id DESC LIMIT 15")
            return self._send(200, {"allow": W.web_allow(), "recent": rows})
        if u.path == "/api/mailbox":
            return self._send(200, W.mailbox())
        if u.path == "/api/transcript":
            return self._send(200, W.transcript(int(qs.get("since", ["0"])[0] or 0)).encode("utf-8"), "text/plain; charset=utf-8")
        if u.path == "/api/architect":
            d = W.architect.overview()
            d["local"] = self._local()
            return self._send(200, d)
        if u.path == "/api/archive":
            return self._send(200, W.archive_overview(qs.get("owner", [""])[0], qs.get("path", [""])[0], qs.get("q", [""])[0]))
        if u.path == "/api/plugins":
            return self._send(200, W.plugins.listing())
        if u.path == "/api/plugin_js":
            return self._send(200, W.plugins.viewer_js())
        if u.path == "/api/agent":
            d = W.agent_detail(qs.get("name", [""])[0])
            return self._send(200 if d else 404, d or {"error": "no such agent"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        global KEY, ONLINE
        if urlparse(self.path).path == "/login":
            return self._login()
        if not self._ok({}):
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        p = urlparse(self.path).path
        if p == "/api/board" and str(body.get("text", "")).strip():
            W.homeowner_board(str(body["text"]))
            return self._send(200, {"ok": True})
        if p == "/api/mail":
            ok = W.homeowner_mail(str(body.get("to", "")), str(body.get("text", "")))
            return self._send(200 if ok else 400, {"ok": ok})
        if p == "/api/mailbox":
            W.mailbox_read()
            return self._send(200, {"ok": True})
        if p == "/api/file":
            ok = W.owner_file(str(body.get("agent", "")), str(body.get("action", "")), str(body.get("path", "")), str(body.get("text", "")), body.get("shared"))
            return self._send(200 if ok else 400, {"ok": ok})
        if p == "/api/quota":
            try:
                return self._send(200, {"mb": W.set_quota(str(body.get("agent", "")), body.get("mb", 500))})
            except Exception:
                return self._send(400, {"error": "bad value"})
        if p == "/api/pause":
            W.set_paused(bool(body.get("paused")))
            return self._send(200, {"paused": W.paused})
        if p == "/api/online":

            if not self._local():
                return self._send(403, {"error": "only on the homeowner's computer"})
            if body.get("on"):
                k = str(body.get("key", "")).strip()
                if len(k) < 4:
                    return self._send(400, {"error": "choose a password of at least 4 characters"})
                KEY, ONLINE = k, True
                SALT_NEW = os.urandom(8).hex()
                globals()["SALT"] = SALT_NEW        # a new password invalidates old logins
                threading.Thread(target=start_tunnel, daemon=True).start()
                W.event("sys", "Homeowner", "Went online: other devices can connect with the session password.", "sys")
            else:
                ONLINE, KEY = False, ""
                stop_tunnel()
                W.event("sys", "Homeowner", "Went offline: only this computer can see the House.", "sys")
            return self._send(200, online_info(True))
        if p == "/api/shutdown":
            def bye():
                try:
                    W.shutdown()
                finally:
                    stop_tunnel()
                    time.sleep(0.4)
                    os._exit(0)
            threading.Thread(target=bye, daemon=True).start()
            return self._send(200, {"ok": True, "note": "Closing gently. It is safe to close this page."})
        if p == "/api/agent_control":
            ok = W.agent_control(str(body.get("name", "")), str(body.get("action", "")))
            return self._send(200 if ok else 400, {"ok": ok})
        if p == "/api/spawn":
            ok = W.spawn_cell(int(body.get("cell", -1)))
            return self._send(200 if ok else 400, {"ok": ok})
        if p == "/api/points":
            try:
                ok = W.homeowner_points(str(body.get("name", "")), body.get("amount", 1))
            except Exception:
                ok = False
            return self._send(200 if ok else 400, {"ok": ok})
        if p == "/api/owner":
            ok = W.owner_move(str(body.get("room", "")))
            return self._send(200 if ok else 400, {"ok": ok})
        if p == "/api/plugin":
            pm, act, nm = W.plugins, str(body.get("action", "")), str(body.get("name", ""))
            if act == "add":
                ok, msg = pm.add(nm, str(body.get("code", "")))
            elif act == "approve":
                ok, msg = pm.approve(nm)
                if ok:
                    W.script_decided(nm, True)
            elif act == "reject":
                W.script_decided(nm, False)
                ok, msg = pm.reject(nm), ""
            elif act == "toggle":
                ok, msg = pm.toggle(nm, bool(body.get("enabled"))), ""
            elif act == "delete":
                ok, msg = pm.delete(nm), ""
            else:
                ok, msg = False, "unknown action"
            return self._send(200 if ok else 400, {"ok": ok, "message": msg})
        if p == "/api/web":
            ok = W.web_allow_set(str(body.get("action", "")), str(body.get("domain", "")))
            return self._send(200 if ok else 400, {"ok": ok, "allow": W.web_allow()})
        if p == "/api/request":
            ok = W.decide_request(int(body.get("id", 0)), str(body.get("decision", "")))
            return self._send(200 if ok else 400, {"ok": ok})
        if p == "/api/architect":
            act, ar = str(body.get("action", "")), W.architect
            if act in ("approved", "declined"):
                ok = W.decide_request(int(body.get("id", 0)), act)
            elif act == "retry":
                ok = W.retry_request(int(body.get("id", 0)))
            elif act == "auto_install":
                ar.set_auto_install(bool(body.get("on"))); ok = True
            elif act in ("set_key", "clear_key"):
                if not self._local():
                    return self._send(403, {"error": "the API key can only be set on the homeowner's own computer"})
                ar.set_key(str(body.get("key", "")) if act == "set_key" else ""); ok = True
            elif act == "limit":
                try:
                    ar.daily_limit = max(0, min(100, int(body.get("n", 5)))); ok = True
                except Exception:
                    ok = False
            else:
                ok = False
            return self._send(200 if ok else 400, {"ok": ok, "status": ar.status()})
        self._send(404, {"error": "not found"})


def main():
    global W
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--tier", type=int, default=1, help="Taskmaster difficulty (see calibrate.py)")
    ap.add_argument("--db", default="house.db")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on; other devices are refused until you press Go online in the viewer (or pass --key)")
    ap.add_argument("--key", default=os.environ.get("HOUSE_KEY", ""), help="start already online with this session password (or set HOUSE_KEY)")
    ap.add_argument("--cooldown", type=int, default=8, help="seconds between one task and the next")
    ap.add_argument("--pass-score", type=int, default=4, help="checks (of 5) needed for a task to count as passed")
    ap.add_argument("--tick", type=float, default=1.0, help="pause between agent turns")
    ap.add_argument("--architect-daily", type=int, default=5, help="most tools the architect builds per day")
    ap.add_argument("--serial", action="store_true", help="one agent turn at a time (default: the Cell and the House take turns in parallel)")
    ap.add_argument("--ctx", type=int, default=12288, help="context window for every call (bigger = longer memory, slower prefill)")
    ap.add_argument("--turn-target", type=float, default=12.0, help="seconds an agent turn should take; memory in the prompt grows or shrinks to stay near it")
    ap.add_argument("--embed-model", default="", help="optional Ollama embedding model (e.g. nomic-embed-text, run: ollama pull nomic-embed-text) so repeats are caught even when reworded")
    ap.add_argument("--fake", action="store_true", help="use a stand-in model (no Ollama)")
    ap.add_argument("--fake-pass", type=float, default=0.6, help="chance the fake model solves a task")
    a = ap.parse_args()
    if a.fake:
        from fake import FakeLLM
        llm = FakeLLM(a.fake_pass)
        a.cooldown = min(a.cooldown, 3)
    else:
        llm = LLM(a.model, a.ollama)
        llm.embed_model = a.embed_model
    llm.num_ctx = a.ctx
    W = World(a.db, llm, n_agents=a.agents, tier=a.tier, cooldown=a.cooldown, tick=a.tick, pass_score=a.pass_score)
    W.turn_target = a.turn_target
    import plugins as plugins_mod
    from world import ACTION_DOC
    plugins_mod.BUILTIN.update(ACTION_DOC)
    W.plugins = plugins_mod.Manager(W, os.path.join(HERE, "plugins"))
    W.plugins.scan()
    W.orient_existing()
    W.backfill_temps()
    threading.Thread(target=W.plugins.run_forever, daemon=True).start()
    W.architect = Architect(W, daily_limit=a.architect_daily)
    try:
        k = W.settle_unbuilt()
        if k:
            print(f"{k} approved request(s) were never built (no architect key): their agents were told.")
    except Exception:
        pass
    threading.Thread(target=W.run_forever, kwargs={'parallel': not a.serial}, daemon=True).start()
    threading.Thread(target=W.architect.run_forever, daemon=True).start()
    global KEY, ONLINE, PORT
    KEY, ONLINE, PORT = a.key, bool(a.key), a.port
    W.reopened()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    if KEY:
        threading.Thread(target=start_tunnel, daemon=True).start()
        print("Online for other devices with your --key password. LAN: " + ", ".join(f"http://{i}:{a.port}" for i in lan_ips()))
    print(f"The House is open: http://localhost:{a.port}   (model: {'fake' if a.fake else a.model})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("closing")


if __name__ == "__main__":
    main()
