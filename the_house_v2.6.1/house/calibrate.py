"""
Measure how your model does on the Taskmaster's tasks, so "medium" is a number, not a guess.

    python calibrate.py                       # defaults below
    python calibrate.py --model qwen3:8b --tiers 0 1 2 --n 8

Goal: one attempt passes (5/5) about 30-50% of the time at your chosen tier. Easier than
that and nothing is learned; harder and agents stay in the Cell. Partial scores matter too:
an average of 3+/5 means the model is thinking, just not perfectly.
"""
import argparse, json, statistics, urllib.error, urllib.request, random
import taskmaster as T

def ask(model, prompt, host, temp):
    body = json.dumps({
        "model": model, "stream": False, "think": True,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": temp, "num_predict": 6000, "num_ctx": 8192},
    }).encode()
    req = urllib.request.Request(host + "/api/chat", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)["message"]["content"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise SystemExit(f"\nOllama answered {e.code}: {detail}\n"
                         f"If it says the model was not found, run `ollama list` and pass the exact name, "
                         f"for example: python calibrate.py --model qwen3:8b")
    except urllib.error.URLError as e:
        raise SystemExit(f"\nCannot reach Ollama at {host} ({e.reason}). Is Ollama running?")

def run(model, tiers, n, host, temp, ask_fn=ask):
    rows = []
    for tier in tiers:
        for fam in sorted(T.FAMILIES):
            scores = []
            for i in range(n):
                task = T.build(fam, tier, random.randrange(2**31))
                raw = ask_fn(model, task.prompt, host, temp)
                import re
                m = T._ANS.search(raw or "")
                try: ans = json.loads(m.group(1)) if m else {}
                except Exception: ans = {}
                if not isinstance(ans, dict): ans = {}
                s = 0
                for _, fn in task.checks:
                    try: s += bool(fn(ans))
                    except Exception: pass
                scores.append(s)
            rows.append((tier, fam, statistics.mean(scores), sum(x == 5 for x in scores) / n))
            print(f"tier {tier}  {fam:<11} avg {rows[-1][2]:.1f}/5   pass {rows[-1][3]:>4.0%}", flush=True)
    print()
    for tier in tiers:
        p = [r[3] for r in rows if r[0] == tier]; a = [r[2] for r in rows if r[0] == tier]
        print(f"TIER {tier}: avg score {statistics.mean(a):.2f}/5, full-pass rate {statistics.mean(p):.0%}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--tiers", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--temp", type=float, default=0.6)
    a = ap.parse_args()
    run(a.model, a.tiers, a.n, a.host, a.temp)
