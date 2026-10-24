"""OpenRouter client: asks free models in order until one returns valid JSON."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API = "https://openrouter.ai/api/v1"


def _key():
    key = os.environ.get("OPENROUTER_API_KEY")
    env = Path(__file__).parent / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("\"'")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing: add it to .env")
    return key


def parse_json(text):
    """Pull the JSON object out of a reply, ignoring ``` fences or chatter around it."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    # raw_decode ignores trailing text; strict=False allows raw newlines inside answers
    return json.JSONDecoder(strict=False).raw_decode(text[start:])[0]


def chat(models, system, user):
    """Try each model in order. Returns (parsed_json, model_id); raises if every model fails."""
    errors = []
    for model in models:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 6000,
            "reasoning": {"effort": "low"},  # free reasoning models otherwise burn the whole budget thinking
        }
        req = urllib.request.Request(
            API + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + _key(), "Content-Type": "application/json", "X-Title": "job-autofill"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.load(r)
            if "error" in resp:
                raise ValueError(resp["error"].get("message", resp["error"]))
            return parse_json(resp["choices"][0]["message"].get("content") or ""), model
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
        except (ValueError, KeyError, IndexError, urllib.error.URLError, TimeoutError) as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("all models failed:\n     " + "\n     ".join(errors))


def list_free():
    with urllib.request.urlopen(API + "/models", timeout=30) as r:
        models = json.load(r)["data"]
    for m in sorted((m for m in models if m["id"].endswith(":free")), key=lambda m: -m.get("context_length", 0)):
        print(f'{m["id"]:55} {m.get("context_length", 0):>10,} context')
