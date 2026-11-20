"""
dynamic_layout_split.py
========================
1-2 piksellik tarama kalıntılarını (gürültü) tolere eden, satır aralarındaki
beyaz boşluklardan kusursuz bölme yapan fine-tune edilmiş modül.
"""

from __future__ import annotations

import re
from pathlib import Path
from PIL import Image

Box = tuple[int, int, int, int]


def _otsu_threshold(histogram: list[int]) -> int:
    total = sum(histogram)
    if total == 0:
        return 128
    sum_total = sum(i * c for i, c in enumerate(histogram))
    weight_bg = 0
    sum_bg = 0.0
    best_thr = 0
    best_var = -1.0
    for t in range(256):
        weight_bg += histogram[t]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += t * histogram[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        between_var = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if between_var > best_var:
            best_var = between_var
            best_thr = t
    return best_thr


def _binarize(gray: Image.Image) -> Image.Image:
    thr = _otsu_threshold(gray.histogram())
    lut = [1 if v < thr else 0 for v in range(256)]
    return gray.point(lut, mode="L")


def _row_profile(binary_region: Image.Image) -> list[int]:
    w, h = binary_region.size
    data = binary_region.tobytes()
    return [sum(data[y * w:(y + 1) * w]) for y in range(h)]


def _col_profile(binary_region: Image.Image) -> list[int]:
    transposed = binary_region.transpose(Image.TRANSPOSE)
    tw, th = transposed.size
    data = transposed.tobytes()
    return [sum(data[x * tw:(x + 1) * tw]) for x in range(th)]


def _find_gaps(profile: list[int], max_val: int = 2, min_gap_len: int = 4) -> list[tuple[int, int]]:
    """
    max_val = 2 yapılarak 1-2 piksellik tarama gürültüleri/kalıntıları 
    boşluk (beyaz alan) olarak kabul edilir.
    """
    runs: list[tuple[int, int]] = []
    n = len(profile)
    i = 0
    while i < n:
        if profile[i] <= max_val:
            start = i
            while i < n and profile[i] <= max_val:
                i += 1
            if (i - start) >= min_gap_len:
                runs.append((start, i))
        else:
            i += 1
    return runs


def _xy_cut(
    binary: Image.Image,
    box: Box,
    *,
    min_block_px: int,
    depth: int = 0,
    max_depth: int = 6,
) -> list[Box]:
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1

    if w < min_block_px * 2 or h < min_block_px or depth >= max_depth:
        return [box]

    region = binary.crop(box)

    # 1. ÖNCELİK: DİKEY KESİM (Sütun arası boşluklar)
    col_prof = _col_profile(region)
    v_gaps = _find_gaps(col_prof, max_val=2, min_gap_len=15)
    valid_v_gaps = [(s, e) for s, e in v_gaps if 0.3 * w < s < 0.7 * w]

    if valid_v_gaps:
        best_gap = max(valid_v_gaps, key=lambda g: g[1] - g[0])
        split_x = best_gap[0] + (best_gap[1] - best_gap[0]) // 2

        left_box = (x1, y1, x1 + split_x, y2)
        right_box = (x1 + split_x, y1, x2, y2)

        boxes = []
        boxes.extend(_xy_cut(binary, left_box, min_block_px=min_block_px, depth=depth + 1, max_depth=max_depth))
        boxes.extend(_xy_cut(binary, right_box, min_block_px=min_block_px, depth=depth + 1, max_depth=max_depth))
        return boxes

    # 2. ÖNCELİK: YATAY KESİM (Satır araları - Fine-tune edilmiş tolerans ile)
    row_prof = _row_profile(region)
    h_gaps = _find_gaps(row_prof, max_val=2, min_gap_len=6)
    valid_h_gaps = [(s, e) for s, e in h_gaps if 0.1 * h < s < 0.9 * h]

    if valid_h_gaps:
        best_gap = max(valid_h_gaps, key=lambda g: g[1] - g[0])
        gap_start, gap_end = best_gap
        
        # En fazla 2 piksellik kalıntı toleransı (Fine-tune kuralı)
        is_safe_white = all(row_prof[y] <= 2 for y in range(gap_start, gap_end))
        
        if is_safe_white:
            split_y = gap_start + (gap_end - gap_start) // 2

            top_box = (x1, y1, x2, y1 + split_y)
            bottom_box = (x1, y1 + split_y, x2, y2)

            boxes = []
            boxes.extend(_xy_cut(binary, top_box, min_block_px=min_block_px, depth=depth + 1, max_depth=max_depth))
            boxes.extend(_xy_cut(binary, bottom_box, min_block_px=min_block_px, depth=depth + 1, max_depth=max_depth))
            return boxes

    return [box]


def dynamic_layout_split(
    image_path: Path,
    split_output_dir: Path,
    *,
    min_block_px: int = 140,    # Küçük bloklara bölünmeyi engellemek için ideal eşik
    pad_px: int = 4,
    min_ink_density: float = 0.02,
) -> list[Path]:
    split_output_dir.mkdir(parents=True, exist_ok=True)

    color_img = Image.open(image_path).convert("RGB")
    gray = color_img.convert("L")
    binary = _binarize(gray)
    w, h = binary.size

    content_box = binary.getbbox()
    if content_box is None:
        return []

    leaf_boxes = _xy_cut(
        binary, content_box,
        min_block_px=min_block_px,
    )

    match = re.search(r'\d+', image_path.stem)
    page_prefix = match.group(0).lstrip('0') if match else "1"
    if not page_prefix:
        page_prefix = "1"

    parts: list[Path] = []
    for (bx1, by1, bx2, by2) in leaf_boxes:
        bx_w = bx2 - bx1
        by_h = by2 - by1

        if by_h < 15 and bx_w > 5 * by_h:
            continue

        region_bin = binary.crop((bx1, by1, bx2, by2))
        area = bx_w * by_h
        if area <= 0:
            continue

        ink_count = sum(region_bin.tobytes())
        if (ink_count / area) < min_ink_density:
            continue

        px1 = max(0, bx1 - pad_px)
        py1 = max(0, by1 - pad_px)
        px2 = min(w, bx2 + pad_px)
        py2 = min(h, by2 + pad_px)

        part_img = color_img.crop((px1, py1, px2, py2))
        
        part_name = f"{page_prefix}_{len(parts) + 1}.png"
        part_path = split_output_dir / part_name
        
        part_img.save(part_path, "PNG")
        parts.append(part_path)

    if not parts:
        out = split_output_dir / f"{page_prefix}_1.png"
        color_img.save(out, "PNG")
        parts.append(out)

    return parts


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("./split_output_isolated")
    result = dynamic_layout_split(src, dst)
    print(f"{len(result)} parça oluşturuldu: {dst}")
    for p in result:
        with Image.open(p) as im:
            print(f"  {p.name}  ({im.width}x{im.height})")
