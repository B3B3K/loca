"""Command line entry: python -m apkagent --apk app.apk --backend ollama --model qwen2.5-coder:14b"""
import argparse
import sys
from pathlib import Path

from . import LLM, Workflow, build_agent, decompile, start_llama_server

C = {"step": "34", "call": "32", "obs": "37", "note": "36", "warn": "33", "phase": "35"}


def reporter(kind, d):
    tty = sys.stdout.isatty()
    c = lambda s, k: f"\033[{C[k]}m{s}\033[0m" if tty else s
    if kind == "token":
        sys.stdout.write(d["text"]); sys.stdout.flush()
    elif kind == "phase":
        print(c(f"\n=== PHASE {d['id']} ===", "phase"))
    elif kind == "step":
        print(c(f"\n-- step {d['n']}/{d['total']} --", "step"))
    elif kind == "call":
        print(c(f"\n  > {d['name']} {d['args']}", "call"))
    elif kind == "observation":
        lines = d["text"].splitlines()
        print(c("\n".join("    " + l for l in lines[:15]) + (f"\n    ... (+{len(lines)-15} lines)" if len(lines) > 15 else ""), "obs"))
    elif kind == "note":
        print(c(f"  [note] {d['text']}", "note"))
    elif kind == "warn":
        print(c(f"[!] {d['text']}", "warn"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="apkagent")
    p.add_argument("--apk"); p.add_argument("--src", help="existing jadx output (skips decompile)")
    p.add_argument("--out", default="out"); p.add_argument("--lang", default="python")
    p.add_argument("--backend", choices=["llama", "ollama"], default="llama")
    p.add_argument("--url", default=None); p.add_argument("--model")
    p.add_argument("--model-file", help="GGUF path: auto-start llama-server")
    p.add_argument("--api-key"); p.add_argument("--ctx", type=int, default=8192)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--max-steps", type=int, default=30); p.add_argument("--max-obs", type=int, default=4000)
    p.add_argument("--no-think", action="store_true")
    p.add_argument("--prompts-dir", help="directory with overriding prompt files")
    p.add_argument("--only", nargs="*", help="run only these phase ids")
    p.add_argument("--shell", action="store_true"); p.add_argument("--unsafe-shell", action="store_true")
    a = p.parse_args(argv)
    if not (a.apk or a.src):
        p.error("--apk or --src required")

    src = Path(a.src) if a.src else decompile(a.apk, Path(a.out) / "jadx_src")
    url = a.url
    if a.model_file:
        url = start_llama_server(a.model_file, ctx=a.ctx)
    url = url or ("http://127.0.0.1:11434" if a.backend == "ollama" else "http://127.0.0.1:8080")
    llm = LLM(url, a.temperature, a.max_tokens, a.api_key, a.model, a.backend, a.ctx)
    if a.no_think:
        llm.think = False
    if a.backend == "llama" and (n := llm.n_ctx()):
        a.ctx = min(a.ctx, n)
    agent, prompts, variables = build_agent(
        a.apk or src, src, Path(a.out) / "sdk", llm, lang=a.lang, prompts_dir=a.prompts_dir,
        ctx=a.ctx, max_steps=a.max_steps, max_obs=a.max_obs, shell=a.shell,
        unsafe_shell=a.unsafe_shell, on_event=reporter)
    Workflow(agent, prompts, variables).run(only=a.only)
    print(f"\nDone. Output: {Path(a.out) / 'sdk'}")


if __name__ == "__main__":
    main()
