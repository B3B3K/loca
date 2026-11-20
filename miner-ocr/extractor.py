"""
DJVU Magazine Pipeline — PaddleOCR Sürümü
==========================================
Qwen2.5-VL yerine PaddleOCR PP-OCRv5 (Kiril) kullanır.
Sunucu, RAM yönetimi, prompt ve base64 bağımlılıkları kaldırıldı.

Yeni özellikler:
  • Oryantasyon tespiti + otomatik döndürme (preprocess içinde)
  • Fotoğraf tespiti (photo_detector.py — matematiksel, model-free)
  • Fotoğraf olan split parçalar OCR'a gönderilmez
"""

import sys
import json
import logging
import shutil
import subprocess
import argparse
import re
import gc
from pathlib import Path
from typing import Optional

from PIL import Image

from dynamic_layout_split import dynamic_layout_split
from photo_detector import is_photo

# ---------------------------------------------------------------------------
# STATİK YAPILANDIRMA
# ---------------------------------------------------------------------------

DEFAULT_DPI              = 150
DEFAULT_MAX_WIDTH        = 1200
DEFAULT_SPLIT_COLUMNS    = True
DEFAULT_MIN_INK_DENSITY  = 0.012
DEFAULT_CONF_THRESHOLD   = 0.50    # OCR güven eşiği
DEFAULT_PHOTO_THRESHOLD  = 0.55    # Fotoğraf skoru eşiği (photo_detector)
DEFAULT_USE_GPU          = True

DJVU_FALLBACK_DIRS = [
    r"C:\Program Files\DjVuLibre",
    r"C:\Program Files (x86)\DjVuLibre",
]

# ---------------------------------------------------------------------------
# PaddleOCR — Lazy singleton (bir kez yükle)
# ---------------------------------------------------------------------------

_ocr_instance = None

def get_ocr(use_gpu: bool = DEFAULT_USE_GPU):
    global _ocr_instance
    if _ocr_instance is None:
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            raise RuntimeError(
                "PaddleOCR kurulu değil.\n"
                "CPU: pip install paddlepaddle paddleocr\n"
                "GPU: pip install paddlepaddle-gpu==3.0.0 "
                "-i https://www.paddlepaddle.org.cn/packages/stable/cu126/ && pip install paddleocr"
            )
        logging.info(f"[OCR] PaddleOCR başlatılıyor (GPU={'Evet' if use_gpu else 'Hayır'})...")
        _ocr_instance = PaddleOCR(
            use_doc_orientation_classify=True,   # 0/90/180/270° tespiti
            use_textline_orientation=True,        # satır bazlı oryantasyon
            use_doc_unwarping=True,              # eğri/kıvrık sayfa düzeltme
            text_recognition_model_name="cyrillic_PP-OCRv5_mobile_rec",
            lang="ru",
            device="gpu" if use_gpu else "cpu",
            enable_mkldnn=False,   
        )
        logging.info("[OCR] PaddleOCR hazır.")
    return _ocr_instance

# ---------------------------------------------------------------------------
# DjVu yardımcıları
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

def render_pages(
    djvu_path: Path,
    pages_dir: Path,
    dpi: int,
    page_range: Optional[tuple[int, int]],
) -> list[Path]:
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
            cmd = [
                ddjvu, "-format=ppm",
                f"-page={n}", f"-scale={dpi}",
                str(djvu_path.resolve()), str(ppm_out.resolve()),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ddjvu çıkış kodu {result.returncode}: {result.stderr.strip()}")
            with Image.open(ppm_out) as img:
                img.save(png_out, "PNG")
            if ppm_out.exists():
                ppm_out.unlink()
        paths.append(png_out)
    return paths

# ---------------------------------------------------------------------------
# Görsel ön işleme: boyut sınırla
# Oryantasyon düzeltmesi PaddleOCR'ın predict() içinde
# (use_doc_orientation_classify=True) otomatik yapılıyor.
# ---------------------------------------------------------------------------

def preprocess(image_path: Path, max_width: int, use_gpu: bool) -> Path:
    img = Image.open(image_path).convert("RGB")
    w, h = img.size

    if w > max_width:
        new_h = int(h * max_width / w)
        img = img.resize((max_width, new_h), Image.LANCZOS)

    out_path = image_path.with_suffix(".pre.png")
    img.save(out_path, "PNG")
    return out_path

# ---------------------------------------------------------------------------
# OCR çağrısı (PaddleOCR 3.x — predict API)
# ---------------------------------------------------------------------------

def call_ocr(
    image_path: Path,
    conf_threshold: float = DEFAULT_CONF_THRESHOLD,
    use_gpu: bool = DEFAULT_USE_GPU,
) -> tuple[str, Optional[str]]:
    """
    Döner: (text, error_or_None)
    text == "NO_TEXT_CONTENT" → içerik yok
    """
    try:
        import numpy as np
        ocr = get_ocr(use_gpu)
        img = Image.open(image_path).convert("RGB")
        arr = np.array(img)

        result = ocr.predict(arr)

        if not result:
            return "NO_TEXT_CONTENT", None

        res = result[0]

        # PaddleOCR 3.x: OCRResult dict-benzeri; alanlar .get ile güvenli okunur
        texts  = res.get("rec_texts")  or []
        scores = res.get("rec_scores") or []

        if not texts:
            return "NO_TEXT_CONTENT", None

        lines = [t for t, c in zip(texts, scores) if c >= conf_threshold]

        if not lines:
            return "NO_TEXT_CONTENT", None

        return "\n".join(lines), None

    except Exception as e:
        return "", str(e)

# ---------------------------------------------------------------------------
# Text temizleme (Markdown artık gelmiyor ama NO_TEXT_CONTENT kontrolü kalıyor)
# ---------------------------------------------------------------------------

def clean_text(raw: str) -> tuple[str, bool]:
    if raw.strip() == "NO_TEXT_CONTENT":
        return "", False
    cleaned = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return cleaned, bool(cleaned)

# ---------------------------------------------------------------------------
# JSON kayıt
# ---------------------------------------------------------------------------

def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Ana işlem döngüsü
# ---------------------------------------------------------------------------

def process_file(
    djvu_path:       Path,
    output_dir:      Path,
    dpi:             int,
    max_width:       int,
    page_range:      Optional[tuple[int, int]],
    split_columns:   bool,
    min_ink_density: float,
    start_page:      int,
    conf_threshold:  float,
    photo_threshold: float,
    use_gpu:         bool,
) -> dict:

    file_dir   = output_dir / djvu_path.stem
    pages_dir  = file_dir / "pages"
    splits_dir = file_dir / "split_parts"
    json_dir   = file_dir / "json_pages"

    file_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    # OCR singleton'ı başlat (ilk çağrıda model indirilir)
    get_ocr(use_gpu)

    logging.info(f"\n{'='*55}\nDosya: {djvu_path.name} (Başlangıç Sayfası: {start_page})\n{'='*55}")

    try:
        png_paths = render_pages(djvu_path, pages_dir, dpi, page_range)
    except Exception as e:
        logging.error(f"[DJVU] Render hatası: {e}")
        return {
            "source_file": str(djvu_path),
            "total_pages": 0,
            "error_count": 1,
            "pages": [{"error": str(e)}],
        }

    pages_summary = []

    for png in png_paths:
        page_num = int(png.stem.split("_")[1])

        if page_num < start_page:
            logging.info(f"[SKIP] Sayfa {page_num} atlanıyor (start-page: {start_page})...")
            continue

        logging.info(f"[PROC] Sayfa {page_num} işleniyor...")

        # Oryantasyon düzelt + boyut sınırla
        proc_png = preprocess(png, max_width, use_gpu)

        # Layout split
        if split_columns:
            sub_parts = dynamic_layout_split(proc_png, splits_dir, min_ink_density=min_ink_density)
        else:
            sub_parts = [proc_png]

        sub_parts = sorted(
            sub_parts,
            key=lambda p: int(re.search(r'_(\d+)\.png$', p.name).group(1))
                          if re.search(r'_(\d+)\.png$', p.name) else 0,
        )

        combined_text   = []
        has_content_any = False
        page_error      = None
        photo_parts     = 0

        for part_idx, part_path in enumerate(sub_parts, 1):
            side_name = f"parça {part_idx}/{len(sub_parts)}"

            # --- Fotoğraf tespiti ---
            try:
                photo, score, signals = is_photo(part_path, threshold=photo_threshold)
            except Exception as e:
                logging.warning(f"[PHOTO] Tespit hatası ({part_path.name}): {e}")
                photo, score = False, 0.0

            if photo:
                photo_parts += 1
                logging.info(
                    f"[PHOTO] Sayfa {page_num} ({side_name}) fotoğraf atlandı "
                    f"(score={score:.2f})"
                )
                continue

            # --- OCR ---
            logging.info(f"[OCR] Sayfa {page_num} ({side_name}: {part_path.name})...")
            raw, error = call_ocr(part_path, conf_threshold, use_gpu)

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
            "source_file":  str(djvu_path.name),
            "page_number":  page_num,
            "has_content":  has_content_any,
            "photo_parts":  photo_parts,
            "text":         full_page_text,
            "error":        page_error,
        }
        _save_json(json_dir / f"page_{page_num:04d}.json", page_json_data)
        pages_summary.append(page_json_data)

        gc.collect()

    result = {
        "source_file":    str(djvu_path),
        "total_pages":    len(pages_summary),
        "json_pages_dir": str(json_dir.resolve()),
    }
    _save_json(file_dir / f"{djvu_path.stem}_summary.json", result)
    return result

# ---------------------------------------------------------------------------
# Batch çalıştırıcı
# ---------------------------------------------------------------------------

def run_batch(
    input_dir:       str,
    output_dir:      str,
    dpi:             int   = DEFAULT_DPI,
    max_width:       int   = DEFAULT_MAX_WIDTH,
    page_range:      Optional[tuple[int, int]] = None,
    split_columns:   bool  = DEFAULT_SPLIT_COLUMNS,
    min_ink_density: float = DEFAULT_MIN_INK_DENSITY,
    start_page:      int   = 1,
    conf_threshold:  float = DEFAULT_CONF_THRESHOLD,
    photo_threshold: float = DEFAULT_PHOTO_THRESHOLD,
    use_gpu:         bool  = DEFAULT_USE_GPU,
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
            dpi             = dpi,
            max_width       = max_width,
            page_range      = page_range,
            split_columns   = split_columns,
            min_ink_density = min_ink_density,
            start_page      = start_page,
            conf_threshold  = conf_threshold,
            photo_threshold = photo_threshold,
            use_gpu         = use_gpu,
        )
        summary_files.append(r)

    summary = {"input_dir": input_dir, "output_dir": output_dir, "files": summary_files}
    _save_json(output_path / "batch_summary.json", summary)
    return summary

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_range(s: str) -> tuple[int, int]:
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Format: START-END (örn: 1-50)")
    return int(parts[0]), int(parts[1])

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    p = argparse.ArgumentParser(description="DJVU → Sayfa JSON (PaddleOCR + Fotoğraf Tespiti)")
    p.add_argument("--input",           required=True,  help="DJVU dosyalarının bulunduğu klasör")
    p.add_argument("--output",          required=True,  help="Çıktı klasörü")
    p.add_argument("--dpi",             type=int,   default=DEFAULT_DPI)
    p.add_argument("--max-width",       type=int,   default=DEFAULT_MAX_WIDTH)
    p.add_argument("--no-split",        action="store_true", help="Sütun bölme kapalı")
    p.add_argument("--min-ink-density", type=float, default=DEFAULT_MIN_INK_DENSITY)
    p.add_argument("--pages",           type=parse_range, default=None, help="Sayfa aralığı: START-END")
    p.add_argument("--start-page",      type=int,   default=1)
    p.add_argument("--conf-threshold",  type=float, default=DEFAULT_CONF_THRESHOLD, help="OCR güven eşiği (0-1)")
    p.add_argument("--photo-threshold", type=float, default=DEFAULT_PHOTO_THRESHOLD, help="Fotoğraf skoru eşiği (0-1)")
    p.add_argument("--cpu",             action="store_true", help="GPU yerine CPU kullan")

    args = p.parse_args()

    result = run_batch(
        input_dir       = args.input,
        output_dir      = args.output,
        dpi             = args.dpi,
        max_width       = args.max_width,
        page_range      = args.pages,
        split_columns   = not args.no_split,
        min_ink_density = args.min_ink_density,
        start_page      = args.start_page,
        conf_threshold  = args.conf_threshold,
        photo_threshold = args.photo_threshold,
        use_gpu         = not args.cpu,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))