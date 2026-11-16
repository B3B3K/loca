"""Minimal tool registry. Tools are plain functions taking keyword args and returning str."""
import inspect


class ToolRegistry:
    def __init__(self):
        self._t = {}

    def register(self, name=None):
        def deco(fn):
            self._t[name or fn.__name__] = fn
            return fn
        return deco

    def add(self, name, fn):
        self._t[name] = fn

    def names(self):
        return sorted(self._t)

    def call(self, name, args):
        fn = self._t.get(name)
        if fn is None:
            return f"error: unknown tool '{name}'. Available: {', '.join(self.names())}"
        try:
            return str(fn(**args))
        except TypeError as e:
            sig = str(inspect.signature(fn))
            return f"error: bad args for {name}{sig}: {e}"
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"


def as_bool(v, default=False):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return default if v is None else bool(v)


def as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
