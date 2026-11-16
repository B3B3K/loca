"""Optional helper to launch llama-server."""
import atexit
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request


def start_llama_server(model, port=8080, ctx=8192, server_bin=None, extra_args="", wait=600):
    exe = server_bin or shutil.which("llama-server")
    if not exe:
        raise FileNotFoundError("llama-server not found")
    log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    proc = subprocess.Popen([exe, "-m", model, "--port", str(port), "-c", str(ctx)] + shlex.split(extra_args),
                            stdout=log, stderr=log)
    atexit.register(proc.terminate)
    url = f"http://127.0.0.1:{port}"
    for _ in range(wait):
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited, see {log.name}")
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200:
                    return url
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError("llama-server not ready")
