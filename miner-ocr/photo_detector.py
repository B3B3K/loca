"""
photo_detector.py
=================
Matematiksel (model-free) fotoğraf / tam sayfa görsel tespiti.
Dijital fotoğrafların yanı sıra taranmış / halftone / 1-bit dergi görsellerini
metin sütunlarından yapısal sinyallerle ayırır.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _crop_content(gray: Image.Image, white_thresh: int = 242) -> Image.Image:
    """Etraftaki beyaz kenar boşluklarını kırpar."""
    mask = gray.point(lambda p: 255 if p < white_thresh else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return gray
    x0, y0, x1, y1 = bbox
    if (x1 - x0) < 32 or (y1 - y0) < 32:
        return gray
    return gray.crop(bbox)


def _binary_tone_ratio(gray: Image.Image) -> float:
    """Piksellerin saf siyah/beyaz uçlarında (0-20 veya 235-255) toplanma oranı."""
    hist = gray.histogram()
    total = sum(hist) or 1
    extreme = sum(hist[:21]) + sum(hist[235:])
    return extreme / total


# ---------------------------------------------------------------------------
# Yapısal Sinyal 1: Yatay projeksiyon satır periyodikliği (Metin tespiti)
# ---------------------------------------------------------------------------

def _text_line_periodicity(content: Image.Image) -> tuple[float, int]:
    """
    Doğal dikey çözünürlükte satır bazlı mürekkep yoğunluğu profilindeki
    periyodik 'satır - satır boşluğu' salınımlarını sayar.
    Metin blokları → yüksek vadi sayısı (yüksek text_line_score)
    Fotoğraflar    → 0-1 vadi (0.0 text_line_score)
    """
    cw, ch = content.size
    if ch < 40 or cw < 40:
        return 0.0, 0

    bdata = content.point(lambda p: 1 if p < 200 else 0).tobytes()
    raw_rows = [sum(bdata[y * cw : (y + 1) * cw]) / cw for y in range(ch)]

    # 3 piksellik yumuşatma (tek piksellik tarama tozunu bastırır)
    smooth = [
        0.25 * raw_rows[max(0, y - 1)] + 0.5 * raw_rows[y] + 0.25 * raw_rows[min(ch - 1, y + 1)]
        for y in range(ch)
    ]

    active = [r for r in smooth if r > 0.02]
    if not active:
        return 0.0, 0

    med_ink = sorted(active)[len(active) // 2]
    if med_ink < 0.05:
        return 0.0, 0

    high_thr = med_ink * 0.75
    low_thr = med_ink * 0.45

    valleys = 0
    state = "above" if smooth[0] >= high_thr else "below"
    for r in smooth[1:]:
        if state == "above" and r < low_thr:
            state = "below"
        elif state == "below" and r > high_thr:
            valleys += 1
            state = "above"

    # 6 veya daha fazla düzenli satır boşluğu güçlü metin bloğu işaretidir
    text_line_score = min(valleys / 6.0, 1.0)
    return text_line_score, valleys


# ---------------------------------------------------------------------------
# Yapısal Sinyal 2: 16x16 Makro Izgara Yoğunluğu & Ton Varyansı
# ---------------------------------------------------------------------------

def _macro_grid_features(content: Image.Image, grid: int = 16) -> tuple[float, float]:
    """
    Görseli grid x grid hücreye böler:
    - solid_dark_ratio: %75'ten fazla koyu piksel içeren hücre oranı
      (Metinde harf araları yüzünden ~0.00; fotoğraflarda gölge/kıyafet yüzünden >0.15)
    - macro_std: hücre yoğunluklarının standart sapması
      (Metin paragrafı homojendir ~0.10; fotoğraflarda açık/koyu bölgeler vardır >0.22)
    """
    cw, ch = content.size
    bdata = content.point(lambda p: 1 if p < 190 else 0).tobytes()

    cell_inks: list[float] = []
    for gy in range(grid):
        y0 = (gy * ch) // grid
        y1 = ((gy + 1) * ch) // grid
        if y1 <= y0:
            continue
        for gx in range(grid):
            x0 = (gx * cw) // grid
            x1 = ((gx + 1) * cw) // grid
            if x1 <= x0:
                continue
            dark = 0
            for y in range(y0, y1):
                row_off = y * cw
                dark += sum(bdata[row_off + x0 : row_off + x1])
            area = (y1 - y0) * (x1 - x0)
            cell_inks.append(dark / area)

    if not cell_inks:
        return 0.0, 0.0

    solid_dark = sum(1 for c in cell_inks if c > 0.75) / len(cell_inks)
    mean_c = sum(cell_inks) / len(cell_inks)
    std_c = math.sqrt(sum((c - mean_c) ** 2 for c in cell_inks) / len(cell_inks))
    return solid_dark, std_c


# ---------------------------------------------------------------------------
# Yapısal Sinyal 3: En Büyük Bağlı Koyu Bileşen Oranı
# ---------------------------------------------------------------------------

def _max_blob_ratio(content: Image.Image, max_dim: int = 160) -> float:
    """
    NEAREST örnekleme ile yapıyı bozmadan en büyük bitişik koyu lekenin
    toplam alana oranını bulur.
    Metinde her harf ayrı küçük bir bileşendir (<0.01);
    fotoğraflarda koyu kütleler büyük tek parça alanlar oluşturur (>0.08).
    """
    cw, ch = content.size
    scale = min(1.0, max_dim / max(cw, ch))
    sw = max(16, int(cw * scale))
    sh = max(16, int(ch * scale))

    small = content.resize((sw, sh), Image.NEAREST)
    grid = list(small.point(lambda p: 1 if p < 180 else 0).tobytes())

    visited = bytearray(sw * sh)
    max_area = 0

    for idx in range(sw * sh):
        if grid[idx] == 1 and not visited[idx]:
            visited[idx] = 1
            stack = [idx]
            area = 0
            while stack:
                cur = stack.pop()
                area += 1
                cy, cx = divmod(cur, sw)
                if cx > 0 and grid[cur - 1] == 1 and not visited[cur - 1]:
                    visited[cur - 1] = 1
                    stack.append(cur - 1)
                if cx + 1 < sw and grid[cur + 1] == 1 and not visited[cur + 1]:
                    visited[cur + 1] = 1
                    stack.append(cur + 1)
                if cy > 0 and grid[cur - sw] == 1 and not visited[cur - sw]:
                    visited[cur - sw] = 1
                    stack.append(cur - sw)
                if cy + 1 < sh and grid[cur + sw] == 1 and not visited[cur + sw]:
                    visited[cur + sw] = 1
                    stack.append(cur + sw)
            if area > max_area:
                max_area = area

    return max_area / (sw * sh)


# ---------------------------------------------------------------------------
# Sinyal 4 & 5: Yerel Entropi ve Kenar Yönü Dağılımı
# ---------------------------------------------------------------------------

def _local_entropy(content: Image.Image, sample: int = 128) -> float:
    img = content.resize((sample, sample), Image.BILINEAR)
    data = img.tobytes()

    entropies = []
    for y in range(1, sample - 1):
        for x in range(1, sample - 1):
            counts: dict[int, int] = {}
            for dy in (-1, 0, 1):
                row_off = (y + dy) * sample + x
                for dx in (-1, 0, 1):
                    v = data[row_off + dx]
                    counts[v] = counts.get(v, 0) + 1
            ent = -sum((c / 9.0) * math.log2(c / 9.0) for c in counts.values())
            entropies.append(ent)

    return sum(entropies) / len(entropies) if entropies else 0.0


def _edge_direction_entropy(content: Image.Image, sample: int = 128) -> float:
    img = content.resize((sample, sample), Image.BILINEAR)
    px = img.tobytes()

    bins = [0] * 8
    total = 0
    for y in range(1, sample - 1):
        for x in range(1, sample - 1):
            gx = (
                -px[(y - 1) * sample + (x - 1)] - 2 * px[y * sample + (x - 1)] - px[(y + 1) * sample + (x - 1)]
                + px[(y - 1) * sample + (x + 1)] + 2 * px[y * sample + (x + 1)] + px[(y + 1) * sample + (x + 1)]
            )
            gy = (
                -px[(y - 1) * sample + (x - 1)] - 2 * px[(y - 1) * sample + x] - px[(y - 1) * sample + (x + 1)]
                + px[(y + 1) * sample + (x - 1)] + 2 * px[(y + 1) * sample + x] + px[(y + 1) * sample + (x + 1)]
            )
            if math.hypot(gx, gy) < 12:
                continue
            angle = math.atan2(gy, gx)
            bin_idx = int((angle + math.pi) / (2 * math.pi) * 8) % 8
            bins[bin_idx] += 1
            total += 1

    if total == 0:
        return 0.0

    return -sum((c / total) * math.log2(c / total) for c in bins if c > 0)


# ---------------------------------------------------------------------------
# Ana fonksiyon
# ---------------------------------------------------------------------------

def is_photo(
    image: Image.Image | Path,
    *,
    threshold: float = 0.55,
) -> tuple[bool, float, dict]:
    if isinstance(image, Path):
        image = Image.open(image)

    gray = image.convert("L")
    w, h = gray.size

    if w * h < 60 * 60:
        return False, 0.0, {"reason": "too_small"}

    aspect = max(w / h, h / w)
    if aspect > 8.0:
        return False, 0.0, {"reason": "extreme_aspect_ratio", "aspect": round(aspect, 2)}

    content = _crop_content(gray)
    binary_ratio = _binary_tone_ratio(content)

    # Yapısal sinyaller
    text_line_score, line_valleys = _text_line_periodicity(content)
    solid_dark_ratio, macro_std = _macro_grid_features(content)
    max_blob_ratio = _max_blob_ratio(content)

    # Doku & kenar sinyalleri
    raw_entropy = _local_entropy(content)
    raw_edge = _edge_direction_entropy(content)

    # Normalizasyonlar (0..1)
    no_text_lines = 1.0 - text_line_score
    dark_mass_score = min(solid_dark_ratio / 0.15, 1.0)
    macro_var_score = min(max(macro_std - 0.12, 0.0) / 0.18, 1.0)
    blob_score = min(max_blob_ratio / 0.08, 1.0)
    ent_score = min(max(raw_entropy - 2.3, 0.0) / 2.2, 1.0)
    edge_score = min(max(raw_edge - 2.0, 0.0) / 0.95, 1.0)

    is_scanned_binary = binary_ratio > 0.70

    if is_scanned_binary:
        # Taranmış dergi / halftone / 1-bit baskı modu
        photo_score = (
            0.30 * no_text_lines
            + 0.25 * blob_score
            + 0.20 * dark_mass_score
            + 0.15 * macro_var_score
            + 0.10 * edge_score
        )
    else:
        # Sürekli tonlu / dijital fotoğraf modu
        photo_score = (
            0.30 * ent_score
            + 0.25 * no_text_lines
            + 0.20 * macro_var_score
            + 0.15 * edge_score
            + 0.10 * blob_score
        )

    signals = {
        "mode":             "scanned/halftone" if is_scanned_binary else "continuous",
        "binary_ratio":     round(binary_ratio, 3),
        "line_valleys":     line_valleys,
        "text_line_score":  round(text_line_score, 3),
        "solid_dark_ratio": round(solid_dark_ratio, 3),
        "macro_std":        round(macro_std, 3),
        "max_blob_ratio":   round(max_blob_ratio, 3),
        "local_entropy":    round(raw_entropy, 3),
        "edge_dir_entropy": round(raw_edge, 3),
        "photo_score":      round(photo_score, 3),
        "threshold":        threshold,
    }

    return photo_score >= threshold, photo_score, signals


if __name__ == "__main__":
    import sys
    path = Path(sys.argv[1])
    result, score, signals = is_photo(path)
    print(f"{'FOTOĞRAF' if result else 'TEXT/MIXED'} — score={score:.3f}")
    for k, v in signals.items():
        print(f"  {k}: {v}")