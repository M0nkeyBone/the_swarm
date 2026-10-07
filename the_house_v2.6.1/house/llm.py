"""Thin Ollama client (standard library only)."""
import json
import re
import urllib.error
import urllib.request

_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLM:
    def __init__(self, model="qwen3:8b", host="http://localhost:11434"):
        self.model, self.host = model, host.rstrip("/")
        self.fake = False
        self.num_ctx = 8192      # one size for every call, so Ollama never reloads the model

    def chat(self, system, user, *, json_mode=False, think=False, max_tokens=350, temperature=0.8, repeat_penalty=1.1, schema=None):
        body = {
            "model": self.model, "stream": False, "think": think, "keep_alive": "30m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": self.num_ctx,
                        "repeat_penalty": repeat_penalty},
        }
        if json_mode:
            # A JSON schema makes Ollama constrain decoding to it (a valid action name, args an object), which a small model
            # needs far more than a big one. Ollama does not show the schema's descriptions to the model, so prompts still
            # describe the shape in words. Old Ollama versions reject a schema: then fall back to plain JSON mode for good.
            body["format"] = schema if (schema and not getattr(self, "no_schema", False)) else "json"
        if getattr(self, "no_think", False):
            body.pop("think", None)
        try:
            out = self._post(body)
        except urllib.error.HTTPError as e:
            # 400 means Ollama refused an option. Two can be refused: a JSON schema (Ollama before 0.5) and "think"
            # (models without a thinking mode, or Ollama before 0.9). Drop the schema first: dropping "think" from a
            # qwen3 call would switch its thinking on and make every turn slow.
            if e.code != 400:
                raise
            out = None
            if isinstance(body.get("format"), dict):
                self.no_schema = True
                body["format"] = "json"
                try:
                    out = self._post(body)
                except urllib.error.HTTPError as e2:
                    if e2.code != 400:
                        raise
            if out is None:
                if "think" not in body:
                    raise
                self.no_think = True
                body.pop("think", None)
                out = self._post(body)
                self.no_schema = False     # it was "think" that was refused; the schema gets another chance next call
        return _THINK.sub("", out).strip()


def _post(self, body):
    """POST with our own connection so a pause can cut it off mid-answer (Ollama stops generating when the socket closes)."""
    import http.client, urllib.parse
    u = urllib.parse.urlparse(self.host + "/api/chat")
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.netloc, timeout=600)
    self._conns = getattr(self, "_conns", set())
    self._conns.add(conn)
    try:
        data = json.dumps(body).encode()
        conn.request("POST", u.path, data, {"Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        if r.status >= 400:
            raise urllib.error.HTTPError(self.host, r.status, r.reason, r.headers, None)
        return json.loads(raw)["message"]["content"]
    finally:
        self._conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass


def cancel_all(self):
    for c in list(getattr(self, "_conns", ())):
        try:
            if c.sock:
                c.sock.shutdown(2)
            c.close()
        except Exception:
            pass


def embed(self, text):
    """Vector for a text from an Ollama embedding model (self.embed_model), or None when not configured or it fails."""
    m = getattr(self, "embed_model", "")
    if not m:
        return None
    cache = self.__dict__.setdefault("_ecache", {})
    k = text[:400]
    if k in cache:
        return cache[k]
    try:
        import http.client, urllib.parse
        u = urllib.parse.urlparse(self.host + "/api/embed")
        conn = http.client.HTTPConnection(u.netloc, timeout=20)
        conn.request("POST", u.path, json.dumps({"model": m, "input": k, "keep_alive": "30m"}).encode(), {"Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        v = json.loads(raw)["embeddings"][0] if r.status < 400 else None
    except Exception:
        v = None
        self.embed_model = ""        # not available: stop trying, the word-overlap check still works
    if len(cache) > 500:
        cache.clear()
    cache[k] = v
    return v


LLM.embed = embed
LLM._post = _post
LLM.cancel_all = cancel_all


def parse_json(text):
    """Pull the first JSON object out of a model reply; return {} if none."""
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}
