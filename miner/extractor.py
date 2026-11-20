"""
DJVU Magazine Pipeline — Tam ve Sabit Yapılandırmalı Sürüm
==========================================================
"""

import sys
import json
import base64
import logging
import shutil
import subprocess
import argparse
import re
import time
from pathlib import Path
from typing import Optional
import ctypes
from ctypes import wintypes

import requests
from PIL import Image

from dynamic_layout_split import dynamic_layout_split

# ---------------------------------------------------------------------------
# STATİK YAPILANDIRMA (Tüm ayarları buradan kolayca değiştirebilirsiniz)
# ---------------------------------------------------------------------------

DEFAULT_SERVER_URL      = "http://127.0.0.1:8080"
DEFAULT_DPI             = 150
DEFAULT_MAX_WIDTH       = 1200
DEFAULT_MAX_TOKENS      = 2048
DEFAULT_TEMPERATURE     = 0.1
DEFAULT_SPLIT_COLUMNS   = True
DEFAULT_MIN_INK_DENSITY = 0.012

# Bellek Sınırı (Byte cinsinden: 20 GB)
MAX_RAM_LIMIT_BYTES     = 20 * 1024 * 1024 * 1024 

DJVU_FALLBACK_DIRS = [
    r"C:\Program Files\DjVuLibre",
    r"C:\Program Files (x86)\DjVuLibre",
]

# ---------------------------------------------------------------------------
# Promptlar
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a precise document transcription assistant for a digital magazine pipeline.

Your task:
- Transcribe ALL readable, complete text from the page image.
- Reproduce the logical reading order: title → subtitle → body → captions.
- Separate logical sections with a blank line.
- Use plain text only. No Markdown formatting whatsoever.

Strict rules you MUST follow:
1. SKIP any text embedded inside a graphic, illustration, logo, chart, diagram, or decorative image. Only transcribe clearly typeset body content.
2. If a word or phrase is partially obscured, cut off at the page edge, or unreadable — omit it entirely. Do NOT guess or complete it.
3. Do NOT describe images, photos, illustrations, or any visual element. Ignore them completely.
4. Do NOT include page numbers, repeated header/footer magazine names, or watermarks.
5. If the page contains no readable body text (full-bleed photo, purely decorative) — respond with exactly: NO_TEXT_CONTENT
6. Never add commentary, disclaimers, or notes. Output only the transcription."""

USER_PROMPT = "Transcribe all complete typeset text from this magazine page. Plain text only, no Markdown."

# ---------------------------------------------------------------------------
# Sunucu Yönetimi ve Bellek Takibi
# ---------------------------------------------------------------------------

def get_total_system_ram_usage() -> int:
    """Tüm sistemdeki kullanılan RAM miktarını byte olarak döner."""
    try:
        import psutil
        return psutil.virtual_memory().used
    except ImportError:
        return 0

def check_and_restart_server_if_needed(server_cmd: Optional[str], server_url: str, force_start: bool = False):
    """Eğer RAM 20GB'ı aştıysa veya force_start True ise llama-server'ı arka planda sessizce başlatır."""
    should_start = force_start
    
    if not force_start and server_cmd:
        used_ram = get_total_system_ram_usage()
        if used_ram > MAX_RAM_LIMIT_BYTES:
            used_gb = used_ram / (1024**3)
            logging.warning(f"\n[BELLEK UYARISI] RAM kullanımı {used_gb:.2f} GB (20GB sınırını aştı!).")
            should_start = True

    if should_start and server_cmd:
        # Eski çalışan sunucu kalıntısını temizle
        subprocess.run(["taskkill", "/f", "/im", "llama-server.exe"], capture_output=True)
        time.sleep(2)

        logging.info("[SERVER] Sunucu arka planda başlatılıyor (loglar gizlendi)...")
        
        # stdout ve stderr DEVNULL ile susturuldu
        import subprocess as sp
        subprocess.Popen(server_cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        
        logging.info("[SERVER] Sunucunun model yüklemesi ve ayağa kalkması için 60 saniye bekleniyor...")
        for remaining in range(60, 0, -1):
            sys.stdout.write(f"\r[SERVER] Kalan süre: {remaining} saniye...   ")
            sys.stdout.flush()
            time.sleep(1)
        print("\n[SERVER] Bekleme süresi tamamlandı, işlemlere başlanıyor.")

# ---------------------------------------------------------------------------
# DjVu ve Görsel Yardımcıları
# ---------------------------------------------------------------------------

def find_djvu_tool(name: str) -> str:
    found = shutil.which(name) or shutil.which(name + ".exe")
    if found:
        return found
    for d in DJVU_FALLBACK_DIRS:
        c = Path(d) / (name + ".exe")
        if c.exists():
            return str(c)
    raise FileNotFoundError(f"'{name}' bulunamadı. DjVuLibre kurulu olduğundan emin olun.")

def get_page_count(djvu_path: Path) -> int:
    djvused = find_djvu_tool("djvused")
    r = subprocess.run([djvused, str(djvu_path), "-e", "n"], capture_output=True, text=True, check=True)
    return int(r.stdout.strip())

def render_pages(djvu_path: Path, pages_dir: Path, dpi: int, page_range: Optional[tuple[int, int]]) -> list[Path]:
    ddjvu = find_djvu_tool("ddjvu")
    pages_dir.mkdir(parents=True, exist_ok=True)

    total = get_page_count(djvu_path)
    start = page_range[0] if page_range else 1
    end   = min(page_range[1], total) if page_range else total

    logging.info(f"[DJVU] {djvu_path.name} — {total} sayfa, işlenecek: {start}–{end}")

    paths = []
    for n in range(start, end + 1):
        ppm_out = pages_dir / f"page_{n:04d}.ppm"
        png_out = pages_dir / f"page_{n:04d}.png"
        
        if not png_out.exists():
            cmd = [ddjvu, "-format=ppm", f"-page={n}", f"-scale={dpi}", str(djvu_path.resolve()), str(ppm_out.resolve())]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ddjvu çıkış kodu {result.returncode}: {result.stderr.strip()}")
            with Image.open(ppm_out) as img:
                img.save(png_out, "PNG")
            if ppm_out.exists():
                ppm_out.unlink()
        paths.append(png_out)
    return paths

def preprocess(image_path: Path, max_width: int) -> Path:
    img = Image.open(image_path)
    w, h = img.size
    if w > max_width:
        new_h = int(h * max_width / w)
        img = img.resize((max_width, new_h), Image.LANCZOS)
        out = image_path.with_suffix(".r.png")
        img.save(out, "PNG")
        return out
    return image_path

def to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()

def call_vl(image_path: Path, server_url: str, max_tokens: int, temperature: float) -> tuple[str, Optional[str]]:
    endpoint = f"{server_url.rstrip('/')}/v1/chat/completions"
    combined_prompt = f"{SYSTEM_PROMPT}\n\n{USER_PROMPT}"
    
    payload = {
        "model": "qwen2.5-vl",
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{to_base64(image_path)}"}},
                {"type": "text", "text": combined_prompt},
            ],
        }],
    }
    try:
        with requests.Session() as session:
            session.headers.update({"Connection": "close"})
            r = session.post(endpoint, json=payload, timeout=600)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip(), None
    except requests.exceptions.Timeout:
        return "", "timeout (600s)"
    except requests.exceptions.ConnectionError:
        return "", f"bağlantı hatası: {endpoint}"
    except Exception as e:
        return "", str(e)

def clean_text(raw: str) -> tuple[str, bool]:
    if raw.strip() == "NO_TEXT_CONTENT":
        return "", False
    text = re.sub(r"^#{1,6}\s+", "", raw, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"^[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    cleaned = text.strip()
    return cleaned, bool(cleaned)

def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# İşlem Döngüsü
# ---------------------------------------------------------------------------

def process_file(
    djvu_path: Path,
    output_dir: Path,
    server_url: str,
    dpi: int,
    max_width: int,
    max_tokens: int,
    temperature: float,
    page_range: Optional[tuple[int, int]],
    split_columns: bool,
    min_ink_density: float,
    start_page: int,
    server_cmd: Optional[str],
) -> dict:

    file_dir  = output_dir / djvu_path.stem
    pages_dir = file_dir / "pages"
    splits_dir = file_dir / "split_parts"
    json_dir = file_dir / "json_pages"
    
    file_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    # İşleme başlamadan ÖNCE sunucuyu zorla başlat ve 60 sn bekle
    if server_cmd:
        check_and_restart_server_if_needed(server_cmd, server_url, force_start=True)

    logging.info(f"\n{'='*55}\nDosya: {djvu_path.name} (Başlangıç Sayfası: {start_page})\n{'='*55}")

    try:
        png_paths = render_pages(djvu_path, pages_dir, dpi, page_range)
    except Exception as e:
        logging.error(f"[DJVU] Render hatası: {e}")
        return {"source_file": str(djvu_path), "total_pages": 0, "error_count": 1, "pages": [{"error": str(e)}]}

    pages_summary = []
    for idx, png in enumerate(png_paths, 1):
        page_num = int(png.stem.split("_")[1])
        
        if page_num < start_page:
            logging.info(f"[SKIP] Sayfa {page_num} atlanıyor (start-page: {start_page})...")
            continue

        # Sayfa başında RAM kontrolü (20GB üstüyse yeniden başlat)
        check_and_restart_server_if_needed(server_cmd, server_url, force_start=False)

        logging.info(f"[PROC] Sayfa {page_num} işleniyor...")

        if split_columns:
            sub_parts = dynamic_layout_split(png, splits_dir, min_ink_density=min_ink_density)
        else:
            sub_parts = [png]

        sub_parts = sorted(sub_parts, key=lambda p: int(re.search(r'_(\d+)\.png$', p.name).group(1)) if re.search(r'_(\d+)\.png$', p.name) else 0)

        combined_text = []
        has_content_any = False
        page_error = None

        for part_idx, part_path in enumerate(sub_parts, 1):
            side_name = f"parça {part_idx}/{len(sub_parts)}"
            logging.info(f"[PROC] Sayfa {page_num} ({side_name}: {part_path.name}) modele gönderiliyor...")

            proc = preprocess(part_path, max_width)
            raw, error = call_vl(proc, server_url, max_tokens, temperature)

            if error:
                logging.warning(f"[ERR] Sayfa {page_num} ({side_name}): {error}")
                page_error = error
                continue

            text, has_content = clean_text(raw)
            if has_content:
                has_content_any = True
                combined_text.append(text)

        full_page_text = "\n\n".join(combined_text)

        page_json_data = {
            "source_file": str(djvu_path.name),
            "page_number": page_num,
            "has_content": has_content_any,
            "text": full_page_text,
            "error": page_error,
        }
        page_json_path = json_dir / f"page_{page_num:04d}.json"
        _save_json(page_json_path, page_json_data)
        pages_summary.append(page_json_data)

        import gc
        gc.collect()

    result = {
        "source_file": str(djvu_path),
        "total_pages": len(pages_summary),
        "json_pages_dir": str(json_dir.resolve()),
    }
    _save_json(file_dir / f"{djvu_path.stem}_summary.json", result)
    return result

# ---------------------------------------------------------------------------
# CLI & Batch
# ---------------------------------------------------------------------------

def run_batch(
    input_dir:     str,
    output_dir:    str,
    server_url:    str   = DEFAULT_SERVER_URL,
    dpi:           int   = DEFAULT_DPI,
    max_width:     int   = DEFAULT_MAX_WIDTH,
    max_tokens:    int   = DEFAULT_MAX_TOKENS,
    temperature:   float = DEFAULT_TEMPERATURE,
    page_range:    Optional[tuple[int, int]] = None,
    split_columns: bool  = DEFAULT_SPLIT_COLUMNS,
    min_ink_density: float = DEFAULT_MIN_INK_DENSITY,
    start_page:    int   = 1,
    server_cmd:    Optional[str] = None,
) -> dict:
    input_path  = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    djvu_files = sorted(input_path.glob("*.djvu"))
    if not djvu_files:
        return {"error": "Hiç .djvu dosyası bulunamadı"}

    summary_files = []
    for djvu in djvu_files:
        r = process_file(
            djvu_path       = djvu,
            output_dir      = output_path,
            server_url      = server_url,
            dpi             = dpi,
            max_width       = max_width,
            max_tokens      = max_tokens,
            temperature     = temperature,
            page_range      = page_range,
            split_columns   = split_columns,
            min_ink_density = min_ink_density,
            start_page      = start_page,
            server_cmd      = server_cmd,
        )
        summary_files.append(r)

    summary = {"input_dir": input_dir, "output_dir": output_dir, "files": summary_files}
    _save_json(output_path / "batch_summary.json", summary)
    return summary

def parse_range(s: str) -> tuple[int, int]:
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Format: START-END")
    return int(parts[0]), int(parts[1])

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    p = argparse.ArgumentParser(description="DJVU → Ayrı Sayfa JSON (Sessiz Sunucu & Otomatik RAM Yönetimi)")
    p.add_argument("--input",       required=True)
    p.add_argument("--output",      required=True)
    p.add_argument("--server",      default=DEFAULT_SERVER_URL)
    p.add_argument("--dpi",         type=int,   default=DEFAULT_DPI)
    p.add_argument("--max-width",   type=int,   default=DEFAULT_MAX_WIDTH)
    p.add_argument("--max-tokens",  type=int,   default=DEFAULT_MAX_TOKENS)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--no-split",    action="store_true")
    p.add_argument("--min-ink-density", type=float, default=DEFAULT_MIN_INK_DENSITY)
    p.add_argument("--pages",       type=parse_range, default=None)
    p.add_argument("--start-page",  type=int, default=1)
    p.add_argument("--server-cmd",  type=str, default=None)

    args = p.parse_args()

    result = run_batch(
        input_dir       = args.input,
        output_dir      = args.output,
        server_url      = args.server,
        dpi             = args.dpi,
        max_width       = args.max_width,
        max_tokens      = args.max_tokens,
        temperature     = args.temperature,
        page_range      = args.pages,
        split_columns   = not args.no_split,
        min_ink_density = args.min_ink_density,
        start_page      = args.start_page,
        server_cmd      = args.server_cmd,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
