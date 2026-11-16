"""Tool-call extraction from (possibly sloppy) model output."""
import json
import re


def parse_call(text, names=None):
    dec = json.JSONDecoder(strict=False)
    starts = []
    m = re.search(r"<tool>", text)
    if m:
        starts.append(m.end())
    starts += [m2.start() for m2 in re.finditer(r"\{", text)]
    for s in starts:
        i = text.find("{", s)
        if i < 0:
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "name" in obj:
            args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"cmd": args}
            return {"name": str(obj["name"]), "args": args if isinstance(args, dict) else {}}
    if names:  # weak models write `tool {json}` inside backticks
        for m in re.finditer(r"`\s*([a-z_0-9]+)\s*(\{.*?\})?\s*`", text, re.S):
            if m.group(1) in names:
                try:
                    args = json.loads(m.group(2), strict=False) if m.group(2) else {}
                except ValueError:
                    continue
                if isinstance(args, dict):
                    return {"name": m.group(1), "args": args}
    return None
