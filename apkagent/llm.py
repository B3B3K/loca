"""LLM client: Ollama native API or llama.cpp (OpenAI-compatible) streaming."""
import json
import urllib.error
import urllib.request


class LLMError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:300]}")
        self.code, self.body = code, body


class LLM:
    def __init__(self, url, temperature=0.2, max_tokens=1024, api_key=None, model=None,
                 backend="llama", ctx=8192):
        self.url = url.rstrip("/")
        self.temperature, self.max_tokens, self.api_key = temperature, max_tokens, api_key
        self.model, self.backend, self.ctx = model, backend, ctx
        self.think = None  # ollama: False => disable thinking

    @property
    def endpoint(self):
        return self.url + ("/api/chat" if self.backend == "ollama" else "/v1/chat/completions")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def n_ctx(self):
        """Server context size (llama.cpp /props); Ollama falls back to configured ctx."""
        if self.backend == "ollama":
            return self.ctx
        try:
            req = urllib.request.Request(self.url + "/props", headers=self._headers())
            with urllib.request.urlopen(req, timeout=5) as r:
                return int(json.load(r)["default_generation_settings"]["n_ctx"])
        except Exception:
            return None

    def chat(self, messages, stop, on_token=lambda t: None):
        if self.backend == "ollama":
            body = {"model": self.model, "messages": messages, "stream": True,
                    "options": {"temperature": self.temperature, "num_predict": self.max_tokens,
                                "num_ctx": self.ctx},
                    "stop": stop, "keep_alive": "10m"}
            if self.think is not None:
                body["think"] = self.think
        else:
            body = {"messages": messages, "stream": True, "temperature": self.temperature,
                    "max_tokens": self.max_tokens, "stop": stop, "cache_prompt": True}
            if self.model:
                body["model"] = self.model
        req = urllib.request.Request(self.endpoint, json.dumps(body).encode(), self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            raise LLMError(e.code, e.read().decode("utf-8", "replace"))
        parts = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if self.backend == "ollama":
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                tok = (d.get("message") or {}).get("content") or ""
                if tok:
                    parts.append(tok)
                    on_token(tok)
                if d.get("done"):
                    break
            else:
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data).get("choices") or []
                except ValueError:
                    continue
                if not ch:
                    continue
                tok = (ch[0].get("delta") or {}).get("content") or ""
                if tok:
                    parts.append(tok)
                    on_token(tok)
        return "".join(parts)
