from __future__ import annotations

import json
import re
import shutil
import tempfile
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import fitz  # type: ignore
from docai_client import process_pdf_with_docai
from full_menu_ocr import FullMenuConfig, FullMenuOCR


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug or "menu"


def create_output_dir(pdf_path: Path, output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    slug = slugify(pdf_path.stem)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_root / f"{slug}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def copy_icon_details(out_dir: Path) -> None:
    src = Path(__file__).resolve().parent / "icon detection details.md"
    if src.exists():
        (out_dir / "icon detection details.md").write_bytes(src.read_bytes())


def cleanup_output(out_dir: Path) -> None:
    # remove per-page raw/structured outputs
    for pattern in ("page_*_raw.json", "page_*_structured.json"):
        for p in out_dir.glob(pattern):
            try:
                p.unlink()
            except Exception:
                pass

    # remove menu_structured if it exists (we keep menu_raw + menu_formatted)
    menu_structured = out_dir / "menu_structured.json"
    if menu_structured.exists():
        try:
            menu_structured.unlink()
        except Exception:
            pass

    # remove debug images
    for p in out_dir.glob("debug_*.png"):
        try:
            p.unlink()
        except Exception:
            pass

    # remove legend folder (legend_icons is kept)
    legend_dir = out_dir / "legend"
    if legend_dir.exists():
        shutil.rmtree(legend_dir, ignore_errors=True)

    # remove input pdf if present
    input_pdf = out_dir / "input.pdf"
    if input_pdf.exists():
        try:
            input_pdf.unlink()
        except Exception:
            pass


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clean_text_line(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_price_like_text(text: Any) -> bool:
    raw = _clean_text_line(text)
    if not raw:
        return False
    lower = raw.lower()
    # Avoid treating timings as prices.
    if re.search(r"\b\d{1,2}:\d{2}\b", lower) and ("am" in lower or "pm" in lower):
        return False
    if re.search(r"(₹|rs\.?|inr|aed|sar|\$|€|£)", lower):
        return True
    # Treat compact kcal/calorie value lines as numeric strips for layout grouping.
    if re.search(r"\b\d{2,4}\s*(kcal|cal)\b", lower):
        return True
    letters = sum(1 for ch in raw if ch.isalpha())
    digits = sum(1 for ch in raw if ch.isdigit())
    if digits <= 0:
        return False
    if "kcal" in lower and digits >= 2 and letters <= 8:
        return True
    if letters == 0:
        return True
    if letters <= 3 and digits >= letters:
        return True
    compact = re.sub(r"\s+", "", raw)
    if re.fullmatch(r"[0-9.,/\-]+", compact):
        return True
    return False


def _norm_comp_text(text: Any) -> str:
    s = _clean_text_line(text).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _line_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _column_text_coverage(ocr_lines: List[str], ref_lines: List[str]) -> float:
    ref_norm = [_norm_comp_text(x) for x in ref_lines]
    ocr_norm = [_norm_comp_text(x) for x in ocr_lines]
    ref_norm = [x for x in ref_norm if len(x) >= 5]
    ocr_norm = [x for x in ocr_norm if len(x) >= 5]
    if not ref_norm:
        return 1.0
    if not ocr_norm:
        return 0.0
    matched = 0
    for r in ref_norm:
        best = 0.0
        for d in ocr_norm:
            sim = _line_similarity(r, d)
            if sim > best:
                best = sim
            if best >= 0.95:
                break
        if best >= 0.55:
            matched += 1
    return matched / max(len(ref_norm), 1)


def _order_lines(lines: List[Dict]) -> List[Dict]:
    return sorted(
        [line for line in lines if isinstance(line, dict)],
        key=lambda l: (
            _to_float((l.get("bbox") or [0, 0, 0, 0])[1]),
            _to_float((l.get("bbox") or [0, 0, 0, 0])[0]),
        ),
    )


def _build_page_text_from_lines(page_no: int, lines: List[Dict], processor: FullMenuOCR) -> str:
    ordered = _order_lines(lines)
    if not ordered:
        return f"[PAGE {page_no}]"
    body_lines: List[Dict] = []
    footer_lines: List[Dict] = []
    for line in ordered:
        text = _clean_text_line(line.get("text"))
        if not text:
            continue
        role = str(line.get("layout_role") or "").strip().lower()
        if role == "footer" or processor._is_footer_text(text):
            footer_lines.append(line)
        else:
            body_lines.append(line)
    parts = [f"[PAGE {page_no}]"]
    if body_lines:
        parts.append("[BODY]")
        for line in processor._order_lines_for_seed(body_lines):
            text = _clean_text_line(line.get("text"))
            if text:
                parts.append(text)
    if footer_lines:
        parts.append("[FOOTER]")
        for line in processor._order_lines_for_seed(footer_lines):
            text = _clean_text_line(line.get("text"))
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _vision_bbox_to_abs(bbox_norm: List[Any], page_w: float, page_h: float) -> List[float] | None:
    if not isinstance(bbox_norm, (list, tuple)) or len(bbox_norm) < 4:
        return None
    vals = [_to_float(v) for v in bbox_norm[:4]]
    # Accept either normalized or absolute values.
    if max(vals) <= 1.5:
        x0, y0, x1, y1 = vals[0] * page_w, vals[1] * page_h, vals[2] * page_w, vals[3] * page_h
    else:
        x0, y0, x1, y1 = vals
    x0 = max(0.0, min(page_w, x0))
    y0 = max(0.0, min(page_h, y0))
    x1 = max(0.0, min(page_w, x1))
    y1 = max(0.0, min(page_h, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _assign_line_to_vision_region(line_bbox: List[Any], regions_abs: List[List[float]]) -> int | None:
    if not isinstance(line_bbox, (list, tuple)) or len(line_bbox) < 4 or not regions_abs:
        return None
    lx0 = _to_float(line_bbox[0])
    ly0 = _to_float(line_bbox[1])
    lx1 = _to_float(line_bbox[2])
    ly1 = _to_float(line_bbox[3])
    cx = (lx0 + lx1) / 2.0
    cy = (ly0 + ly1) / 2.0
    lh = max(1.0, ly1 - ly0)

    # 1) strict containment by center.
    for idx, rb in enumerate(regions_abs):
        if rb[0] <= cx <= rb[2] and rb[1] <= cy <= rb[3]:
            return idx

    # 2) nearest horizontal distance among y-overlapping regions.
    best_idx = None
    best_dist = None
    for idx, rb in enumerate(regions_abs):
        y_overlap = max(0.0, min(ly1, rb[3]) - max(ly0, rb[1]))
        if y_overlap <= 0 and abs(cy - ((rb[1] + rb[3]) / 2.0)) > (lh * 2.0):
            continue
        if cx < rb[0]:
            dx = rb[0] - cx
        elif cx > rb[2]:
            dx = cx - rb[2]
        else:
            dx = 0.0
        if best_dist is None or dx < best_dist:
            best_dist = dx
            best_idx = idx
    if best_idx is not None:
        return best_idx

    # 3) fallback nearest region center.
    best_idx = None
    best_dist = None
    for idx, rb in enumerate(regions_abs):
        rcx = (rb[0] + rb[2]) / 2.0
        dx = abs(cx - rcx)
        if best_dist is None or dx < best_dist:
            best_dist = dx
            best_idx = idx
    return best_idx


def _apply_vision_layout_to_page(page_data: Dict, vision_layout: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(page_data, dict) or not isinstance(vision_layout, dict):
        return None
    lines = page_data.get("lines", [])
    if not isinstance(lines, list) or not lines:
        return None
    ordered = _order_lines(lines)
    if not ordered:
        return None
    page_w = max((_to_float((l.get("bbox") or [0, 0, 0, 0])[2]) for l in ordered), default=0.0)
    page_h = max((_to_float((l.get("bbox") or [0, 0, 0, 0])[3]) for l in ordered), default=0.0)
    if page_w <= 0 or page_h <= 0:
        return None

    regions = vision_layout.get("regions")
    if not isinstance(regions, list) or len(regions) < 2:
        return None
    regions_sorted = sorted(
        [r for r in regions if isinstance(r, dict)],
        key=lambda r: (
            int(r.get("reading_order") or 0),
            _to_float(((r.get("bbox_norm") or [0, 0, 0, 0])[0]), 0.0),
        ),
    )
    regions_abs: List[List[float]] = []
    for reg in regions_sorted:
        bbox = _vision_bbox_to_abs(reg.get("bbox_norm") or reg.get("bbox") or [], page_w, page_h)
        if bbox is None:
            continue
        regions_abs.append(bbox)
    if len(regions_abs) < 2:
        return None

    out_lines: List[Dict[str, Any]] = []
    body_counts: Dict[int, int] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        out_line = dict(line)
        text = _clean_text_line(out_line.get("text"))
        role = str(out_line.get("layout_role") or "").strip().lower()
        if not text or role in {"footer", "shared"}:
            out_lines.append(out_line)
            continue
        region_idx = _assign_line_to_vision_region(out_line.get("bbox") or [0, 0, 0, 0], regions_abs)
        if region_idx is None:
            out_lines.append(out_line)
            continue
        col_no = region_idx + 1
        out_line["layout_role"] = "body"
        out_line["column_index"] = col_no
        out_line["column_bbox"] = [round(regions_abs[region_idx][0], 1), round(regions_abs[region_idx][2], 1)]
        out_lines.append(out_line)
        body_counts[col_no] = body_counts.get(col_no, 0) + 1

    strong_cols = [k for k, v in body_counts.items() if v >= 3]
    if len(strong_cols) < 2:
        return None
    return {"page": page_data.get("page"), "lines": out_lines}


def _compute_page_column_regions(page: Dict) -> List[Dict[str, Any]]:
    lines = page.get("lines", []) if isinstance(page, dict) else []
    ordered = _order_lines(lines)
    if not ordered:
        return []

    page_w = max((_to_float((l.get("bbox") or [0, 0, 0, 0])[2]) for l in ordered), default=0.0)
    page_h = max((_to_float((l.get("bbox") or [0, 0, 0, 0])[3]) for l in ordered), default=0.0)
    if page_w <= 0 or page_h <= 0:
        return []

    grouped: Dict[int, List[Dict]] = {}
    heights: List[float] = []
    shared_lines: List[Dict] = []
    for line in ordered:
        text = _clean_text_line(line.get("text"))
        if not text:
            continue
        role = str(line.get("layout_role") or "").strip().lower()
        if role == "footer":
            continue
        if role == "shared":
            shared_lines.append(line)
            continue
        raw_col = line.get("column_index")
        if not isinstance(raw_col, (int, float)):
            continue
        col_idx = int(raw_col)
        if col_idx <= 0:
            continue
        bbox = line.get("bbox") or [0, 0, 0, 0]
        h = _to_float(bbox[3]) - _to_float(bbox[1])
        if h > 0:
            heights.append(h)
        grouped.setdefault(col_idx, []).append(line)

    if len(grouped) < 2:
        return []

    median_h = 22.0
    if heights:
        heights_sorted = sorted(heights)
        median_h = heights_sorted[len(heights_sorted) // 2]
    pad_x = max(6.0, page_w * 0.008, median_h * 0.5)
    pad_y = max(6.0, median_h * 1.6)

    shared_top = None
    if shared_lines:
        y0_vals = [_to_float((l.get("bbox") or [0, 0, 0, 0])[1]) for l in shared_lines]
        if y0_vals:
            shared_top = min(y0_vals)

    stats: List[Dict[str, Any]] = []
    for col_idx in sorted(grouped.keys()):
        col_lines = grouped[col_idx]
        x0s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[0]) for l in col_lines]
        y0s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[1]) for l in col_lines]
        x1s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[2]) for l in col_lines]
        y1s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[3]) for l in col_lines]
        if not x0s or not x1s or not y0s or not y1s:
            continue
        x0 = min(x0s)
        x1 = max(x1s)
        y0 = min(y0s)
        y1 = max(y1s)
        stats.append(
            {
                "column": col_idx,
                "x0": x0,
                "x1": x1,
                "y0": y0,
                "y1": y1,
                "xc": (x0 + x1) / 2.0,
                "lines": col_lines,
            }
        )

    if len(stats) < 2:
        return []
    stats.sort(key=lambda s: s["xc"])

    def _merge_stat_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
        merged_lines = list(dst.get("lines") or [])
        merged_lines.extend(src.get("lines") or [])
        dst["lines"] = merged_lines
        x0s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[0]) for l in merged_lines]
        y0s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[1]) for l in merged_lines]
        x1s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[2]) for l in merged_lines]
        y1s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[3]) for l in merged_lines]
        if x0s:
            dst["x0"] = min(x0s)
            dst["y0"] = min(y0s)
            dst["x1"] = max(x1s)
            dst["y1"] = max(y1s)
            dst["xc"] = (float(dst["x0"]) + float(dst["x1"])) / 2.0

    # Merge nested/overlapping pseudo-columns (often created by indented name lines).
    changed = True
    while changed and len(stats) > 1:
        changed = False
        total_lines = sum(len(s.get("lines") or []) for s in stats)
        for idx, st in enumerate(list(stats)):
            lines_in_col = st.get("lines") or []
            if not lines_in_col:
                continue
            width = float(st["x1"]) - float(st["x0"])
            height = max(1.0, float(st["y1"]) - float(st["y0"]))
            line_count = len(lines_in_col)
            if width > (page_w * 0.36) and line_count > max(10, int(total_lines * 0.35)):
                continue

            best_j = None
            best_score = 0.0
            for j, other in enumerate(stats):
                if j == idx:
                    continue
                other_w = max(1.0, float(other["x1"]) - float(other["x0"]))
                other_h = max(1.0, float(other["y1"]) - float(other["y0"]))
                ov_x = max(0.0, min(float(st["x1"]), float(other["x1"])) - max(float(st["x0"]), float(other["x0"])))
                ov_y = max(0.0, min(float(st["y1"]), float(other["y1"])) - max(float(st["y0"]), float(other["y0"])))
                if ov_x <= 0 or ov_y <= 0:
                    continue
                x_ratio = ov_x / max(1.0, min(width, other_w))
                y_ratio = ov_y / max(1.0, min(height, other_h))
                if x_ratio < 0.72 or y_ratio < 0.35:
                    continue
                containment = (
                    float(st["x0"]) >= (float(other["x0"]) - 8.0)
                    and float(st["x1"]) <= (float(other["x1"]) + 8.0)
                )
                if (not containment) and x_ratio < 0.86:
                    continue
                score = (x_ratio * 0.7) + (y_ratio * 0.3)
                if score > best_score:
                    best_score = score
                    best_j = j
            if best_j is None:
                continue

            other = stats[best_j]
            st_w = float(st["x1"]) - float(st["x0"])
            other_w = float(other["x1"]) - float(other["x0"])
            if other_w >= st_w:
                _merge_stat_into(other, st)
                stats.pop(idx)
            else:
                _merge_stat_into(st, other)
                stats.pop(best_j)
            stats.sort(key=lambda s: s["xc"])
            changed = True
            break

    # Price-strip guard: merge narrow numeric-heavy pseudo-columns into nearest content column.
    changed = True
    while changed and len(stats) > 1:
        changed = False
        total_lines = sum(len(s.get("lines") or []) for s in stats)
        for idx, st in enumerate(list(stats)):
            lines_in_col = st.get("lines") or []
            if not lines_in_col:
                continue
            width = float(st["x1"]) - float(st["x0"])
            line_count = len(lines_in_col)
            y0_vals = [_to_float((l.get("bbox") or [0, 0, 0, 0])[1]) for l in lines_in_col]
            footerish_strip = bool(y0_vals) and min(y0_vals) >= (page_h * 0.62)
            price_like = 0
            alpha_rich = 0
            for line in lines_in_col:
                text = _clean_text_line(line.get("text"))
                if not text:
                    continue
                if _is_price_like_text(text):
                    price_like += 1
                low = text.lower()
                if re.search(r"[A-Za-z]{3,}", text) and not re.search(r"\b(kcal|cal)\b", low):
                    alpha_rich += 1
            price_ratio = price_like / max(line_count, 1)
            alpha_ratio = alpha_rich / max(line_count, 1)

            # A strip that is mostly numeric and narrow should not be treated as a separate column block.
            is_price_strip = (
                width <= (page_w * 0.22)
                and line_count <= max(10, int(total_lines * 0.35))
                and price_ratio >= 0.7
                and alpha_ratio <= 0.35
            )
            is_footer_aux_strip = (
                width <= (page_w * 0.26)
                and line_count <= max(8, int(total_lines * 0.25))
                and footerish_strip
            )
            is_price_strip = is_price_strip or is_footer_aux_strip
            if not is_price_strip:
                continue

            if len(stats) <= 1:
                break
            if idx == 0:
                merge_idx = 1
            elif idx == (len(stats) - 1):
                merge_idx = idx - 1
            else:
                left_dist = abs(float(st["xc"]) - float(stats[idx - 1]["xc"]))
                right_dist = abs(float(st["xc"]) - float(stats[idx + 1]["xc"]))
                merge_idx = idx - 1 if left_dist <= right_dist else idx + 1

            tgt = stats[merge_idx]
            tgt_lines = tgt.get("lines") or []
            tgt_lines.extend(lines_in_col)
            tgt["lines"] = tgt_lines
            x0s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[0]) for l in tgt_lines]
            y0s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[1]) for l in tgt_lines]
            x1s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[2]) for l in tgt_lines]
            y1s = [_to_float((l.get("bbox") or [0, 0, 0, 0])[3]) for l in tgt_lines]
            tgt["x0"] = min(x0s) if x0s else tgt["x0"]
            tgt["y0"] = min(y0s) if y0s else tgt["y0"]
            tgt["x1"] = max(x1s) if x1s else tgt["x1"]
            tgt["y1"] = max(y1s) if y1s else tgt["y1"]
            tgt["xc"] = (float(tgt["x0"]) + float(tgt["x1"])) / 2.0
            stats.pop(idx)
            stats.sort(key=lambda s: s["xc"])
            changed = True
            break

    regions: List[Dict[str, Any]] = []
    for i, st in enumerate(stats):
        left = max(0.0, st["x0"] - pad_x)
        right = min(page_w, st["x1"] + pad_x)
        if i > 0:
            prev = stats[i - 1]
            left = max(left, (prev["x1"] + st["x0"]) / 2.0)
        if i < (len(stats) - 1):
            nxt = stats[i + 1]
            right = min(right, (st["x1"] + nxt["x0"]) / 2.0)
        top = max(0.0, st["y0"] - (pad_y * 1.6))
        if shared_top is not None:
            top = max(0.0, min(top, shared_top - (pad_y * 0.6)))
        bottom = min(page_h, st["y1"] + (pad_y * 1.8))
        if (right - left) < max(32.0, page_w * 0.08):
            continue
        if (bottom - top) < max(42.0, page_h * 0.2):
            top = 0.0
            bottom = page_h
        regions.append(
            {
                "column": int(st["column"]),
                "bbox": [round(left, 1), round(top, 1), round(right, 1), round(bottom, 1)],
                "lines": st["lines"],
            }
        )

    # Final safety: if a region still looks like a detached price strip, merge it.
    changed = True
    while changed and len(regions) > 1:
        changed = False
        for idx, region in enumerate(list(regions)):
            bbox = region.get("bbox") or [0, 0, 0, 0]
            width = _to_float(bbox[2]) - _to_float(bbox[0])
            lines_in_region = region.get("lines") or []
            if not lines_in_region:
                continue
            price_like = sum(1 for l in lines_in_region if _is_price_like_text((l or {}).get("text")))
            alpha_rich = sum(
                1
                for l in lines_in_region
                if (
                    re.search(r"[A-Za-z]{3,}", _clean_text_line((l or {}).get("text")))
                    and not re.search(r"\b(kcal|cal)\b", _clean_text_line((l or {}).get("text")).lower())
                )
            )
            price_ratio = price_like / max(len(lines_in_region), 1)
            alpha_ratio = alpha_rich / max(len(lines_in_region), 1)
            footerish_strip = _to_float(bbox[1]) >= (page_h * 0.62)
            is_price_strip = width <= (page_w * 0.22) and price_ratio >= 0.75 and alpha_ratio <= 0.35
            is_footer_aux_strip = width <= (page_w * 0.26) and len(lines_in_region) <= 8 and footerish_strip
            if not (is_price_strip or is_footer_aux_strip):
                continue

            if idx == 0:
                merge_idx = 1
            elif idx == (len(regions) - 1):
                merge_idx = idx - 1
            else:
                cur_cx = (_to_float(bbox[0]) + _to_float(bbox[2])) / 2.0
                left_bbox = regions[idx - 1].get("bbox") or [0, 0, 0, 0]
                right_bbox = regions[idx + 1].get("bbox") or [0, 0, 0, 0]
                left_cx = (_to_float(left_bbox[0]) + _to_float(left_bbox[2])) / 2.0
                right_cx = (_to_float(right_bbox[0]) + _to_float(right_bbox[2])) / 2.0
                merge_idx = idx - 1 if abs(cur_cx - left_cx) <= abs(cur_cx - right_cx) else idx + 1

            tgt = regions[merge_idx]
            tgt_bbox = tgt.get("bbox") or [0, 0, 0, 0]
            merged_bbox = [
                round(min(_to_float(tgt_bbox[0]), _to_float(bbox[0])), 1),
                round(min(_to_float(tgt_bbox[1]), _to_float(bbox[1])), 1),
                round(max(_to_float(tgt_bbox[2]), _to_float(bbox[2])), 1),
                round(max(_to_float(tgt_bbox[3]), _to_float(bbox[3])), 1),
            ]
            tgt["bbox"] = merged_bbox
            merged_lines = list(tgt.get("lines") or [])
            merged_lines.extend(lines_in_region)
            tgt["lines"] = merged_lines
            regions.pop(idx)
            changed = True
            break

    # Re-number column ids by left-to-right region order.
    regions.sort(key=lambda r: _to_float((r.get("bbox") or [0, 0, 0, 0])[0]))
    for idx, region in enumerate(regions, start=1):
        region["column"] = idx

    return regions


def _profile_page_layout_complexity(page_data: Dict, processor: FullMenuOCR) -> Dict[str, Any]:
    page_no_raw = page_data.get("page") if isinstance(page_data, dict) else None
    try:
        page_no = int(page_no_raw) if page_no_raw is not None else None
    except Exception:
        page_no = None

    lines = page_data.get("lines", []) if isinstance(page_data, dict) else []
    if not isinstance(lines, list) or not lines:
        return {
            "page": page_no,
            "layout_type": "empty",
            "body_lines": 0,
            "shared_lines": 0,
            "footer_lines": 0,
            "explicit_columns": 0,
            "regions": 0,
        }

    ordered = _order_lines(lines)
    page_w = max((_to_float((l.get("bbox") or [0, 0, 0, 0])[2]) for l in ordered), default=0.0)
    page_h = max((_to_float((l.get("bbox") or [0, 0, 0, 0])[3]) for l in ordered), default=0.0)
    body_count = 0
    shared_count = 0
    footer_count = 0
    explicit_cols: set[int] = set()
    price_like_count = 0

    for line in ordered:
        if not isinstance(line, dict):
            continue
        text = _clean_text_line(line.get("text"))
        if not text:
            continue
        role = str(line.get("layout_role") or "").strip().lower()
        if role == "footer" or processor._is_footer_text(text):
            footer_count += 1
            continue
        body_count += 1
        if role == "shared":
            shared_count += 1
        if _is_price_like_text(text):
            price_like_count += 1
        raw_col = line.get("column_index")
        if isinstance(raw_col, (int, float)):
            col_idx = int(raw_col)
            if col_idx > 0:
                explicit_cols.add(col_idx)

    regions = _compute_page_column_regions(page_data)
    region_count = len(regions)
    if region_count >= 2 or len(explicit_cols) >= 2:
        layout_type = "multi_column"
    elif body_count > 0:
        layout_type = "single_column"
    elif footer_count > 0:
        layout_type = "footer_only"
    else:
        layout_type = "empty"

    out: Dict[str, Any] = {
        "page": page_no,
        "layout_type": layout_type,
        "body_lines": body_count,
        "shared_lines": shared_count,
        "footer_lines": footer_count,
        "price_like_body_lines": price_like_count,
        "explicit_columns": len(explicit_cols),
        "regions": region_count,
    }
    if page_w > 0 and page_h > 0:
        out["width"] = round(page_w, 1)
        out["height"] = round(page_h, 1)
    return out


def _summarize_layout_complexity(menu_raw: Dict, processor: FullMenuOCR, text_source: str) -> Dict[str, Any]:
    pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
    embedded_text_pdf = str(text_source or "").strip().lower() == "pdf"
    if not isinstance(pages, list) or not pages:
        recommended = "native_pdf_text" if embedded_text_pdf else "standard_docai_text"
        return {
            "complexity": "unknown",
            "document_layout": "unknown",
            "embedded_text_pdf": embedded_text_pdf,
            "has_multi_column_pages": False,
            "use_layout_path": False,
            "recommended_strategy": recommended,
            "pages": [],
        }

    page_profiles: List[Dict[str, Any]] = []
    multi_pages = 0
    single_pages = 0
    footer_only_pages = 0
    empty_pages = 0

    for page in pages:
        if not isinstance(page, dict):
            continue
        profile = _profile_page_layout_complexity(page, processor)
        page_profiles.append(profile)
        layout_type = str(profile.get("layout_type") or "")
        if layout_type == "multi_column":
            multi_pages += 1
        elif layout_type == "single_column":
            single_pages += 1
        elif layout_type == "footer_only":
            footer_only_pages += 1
        else:
            empty_pages += 1

    has_multi = multi_pages > 0
    use_layout = has_multi
    if has_multi:
        complexity = "complex"
        document_layout = "multi_column"
        recommended = "layout_segment_then_region_ocr"
    elif single_pages > 0:
        complexity = "simple"
        document_layout = "single_column"
        recommended = "native_pdf_text" if embedded_text_pdf else "standard_docai_text"
    elif footer_only_pages > 0:
        complexity = "simple"
        document_layout = "footer_only"
        recommended = "native_pdf_text" if embedded_text_pdf else "standard_docai_text"
    elif empty_pages > 0:
        complexity = "simple"
        document_layout = "empty"
        recommended = "native_pdf_text" if embedded_text_pdf else "standard_docai_text"
    else:
        complexity = "unknown"
        document_layout = "unknown"
        recommended = "native_pdf_text" if embedded_text_pdf else "standard_docai_text"

    return {
        "complexity": complexity,
        "document_layout": document_layout,
        "embedded_text_pdf": embedded_text_pdf,
        "has_multi_column_pages": has_multi,
        "use_layout_path": use_layout,
        "recommended_strategy": recommended,
        "page_counts": {
            "total": len(page_profiles),
            "multi_column": multi_pages,
            "single_column": single_pages,
            "footer_only": footer_only_pages,
            "empty": empty_pages,
        },
        "pages": page_profiles,
    }


def _ocr_docai_region_text(
    page: Any,
    bbox: List[float],
    processor: FullMenuOCR,
    tmp_dir: Path,
    page_no: int,
    column_no: int,
    dpi: int,
) -> str:
    rect = fitz.Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    if rect.width <= 2 or rect.height <= 2:
        return ""
    pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
    if pix.width <= 8 or pix.height <= 8:
        return ""

    png_bytes = pix.tobytes("png")
    crop_pdf = fitz.open()
    try:
        out_page = crop_pdf.new_page(width=float(pix.width), height=float(pix.height))
        out_page.insert_image(out_page.rect, stream=png_bytes)
        crop_pdf_path = tmp_dir / f"p{page_no:03d}_c{column_no:02d}.pdf"
        crop_pdf.save(str(crop_pdf_path))
    finally:
        crop_pdf.close()

    docai = process_pdf_with_docai(crop_pdf_path)
    docai_doc = processor._docai_get_document(docai)
    docai_pages = docai_doc.get("pages", []) if isinstance(docai_doc, dict) else []
    lines = processor._docai_extract_lines(
        docai_doc=docai_doc,
        docai_pages=docai_pages,
        page_index=0,
        image_shape=(int(pix.height), int(pix.width), 3),
    )
    if lines:
        ordered = processor._order_lines_for_seed(lines)
        out_lines: List[str] = []
        for line in ordered:
            text = _clean_text_line(line.get("text"))
            if text:
                out_lines.append(text)
        if out_lines:
            return "\n".join(out_lines).strip()

    raw = _clean_text_line(processor.docai_raw_text(docai))
    return raw


def _build_region_docai_raw_text(
    input_pdf: Path,
    menu_raw: Dict,
    processor: FullMenuOCR,
    out_dir: Path,
    dpi: int,
) -> tuple[str, Dict[str, Any], bool]:
    pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
    if not pages:
        return "", {"pages": []}, False

    page_chunks: List[str] = []
    debug_pages: List[Dict[str, Any]] = []
    used_region_ocr = False

    with fitz.open(input_pdf) as doc:
        with tempfile.TemporaryDirectory(prefix="region_docai_", dir=str(out_dir)) as tmp:
            tmp_dir = Path(tmp)
            for page_idx, page_data in enumerate(pages):
                if not isinstance(page_data, dict):
                    continue
                page_no = int(page_data.get("page") or (page_idx + 1))
                lines = page_data.get("lines", [])
                if not isinstance(lines, list) or not lines:
                    continue
                if page_idx >= len(doc):
                    page_chunks.append(_build_page_text_from_lines(page_no, lines, processor))
                    debug_pages.append({"page": page_no, "mode": "fallback_missing_page"})
                    continue

                doc_page = doc[page_idx]
                ordered = _order_lines(lines)
                shared_lines = [
                    line for line in ordered if str(line.get("layout_role") or "").strip().lower() == "shared"
                ]
                footer_lines = [
                    line for line in ordered if str(line.get("layout_role") or "").strip().lower() == "footer"
                ]
                vision_layout = None
                vision_used = False
                regions: List[Dict[str, Any]] = []
                if getattr(processor.config, "vision_layout_enabled", False):
                    try:
                        vision_dpi = max(120, int(getattr(processor.config, "vision_layout_dpi", 170)))
                        vision_pix = doc_page.get_pixmap(dpi=vision_dpi, alpha=False)
                        vision_layout = processor.suggest_page_layout_with_openai_vision(
                            page_image_png=vision_pix.tobytes("png"),
                            page_no=page_no,
                        )
                        if isinstance(vision_layout, dict) and bool(vision_layout.get("is_multi_column")):
                            page_with_vision = _apply_vision_layout_to_page(page_data, vision_layout)
                            if page_with_vision:
                                regions = _compute_page_column_regions(page_with_vision)
                                if len(regions) >= 2:
                                    vision_used = True
                    except Exception:
                        vision_layout = None
                if len(regions) < 2:
                    regions = _compute_page_column_regions(page_data)
                if len(regions) < 2:
                    page_chunks.append(_build_page_text_from_lines(page_no, lines, processor))
                    page_debug = {"page": page_no, "mode": "single_column_fallback"}
                    if isinstance(vision_layout, dict):
                        page_debug["vision_layout"] = {
                            "is_multi_column": bool(vision_layout.get("is_multi_column")),
                            "confidence": vision_layout.get("confidence"),
                            "region_count": len(vision_layout.get("regions") or []),
                        }
                    debug_pages.append(page_debug)
                    continue

                parts: List[str] = [f"[PAGE {page_no}]"]
                if shared_lines:
                    parts.append("[SHARED]")
                    for line in processor._order_lines_for_seed(shared_lines):
                        text = _clean_text_line(line.get("text"))
                        if text:
                            parts.append(text)

                page_debug: Dict[str, Any] = {
                    "page": page_no,
                    "mode": ("multi_column_region_docai_vision" if vision_used else "multi_column_region_docai"),
                    "columns": [],
                    "vision_layout_used": vision_used,
                }
                if isinstance(vision_layout, dict):
                    page_debug["vision_layout"] = {
                        "is_multi_column": bool(vision_layout.get("is_multi_column")),
                        "confidence": vision_layout.get("confidence"),
                        "region_count": len(vision_layout.get("regions") or []),
                    }
                for region_idx, region in enumerate(regions, start=1):
                    source_col_no = int(region.get("column") or region_idx)
                    col_no = int(region_idx)
                    bbox = region.get("bbox") or [0, 0, 0, 0]
                    column_lines = region.get("lines") or []
                    text = ""
                    source = "docai_region"
                    error_text = None
                    try:
                        text = _ocr_docai_region_text(
                            page=doc_page,
                            bbox=[float(v) for v in bbox[:4]],
                            processor=processor,
                            tmp_dir=tmp_dir,
                            page_no=page_no,
                            column_no=source_col_no,
                            dpi=dpi,
                        )
                    except Exception as exc:
                        error_text = str(exc)
                        text = ""

                    text_lines = [ln for ln in [_clean_text_line(x) for x in str(text or "").splitlines()] if ln]
                    fallback_lines = []
                    for line in processor._order_lines_for_seed(column_lines):
                        t = _clean_text_line(line.get("text"))
                        if t:
                            fallback_lines.append(t)

                    if not text_lines or len(text_lines) < max(2, int(len(fallback_lines) * 0.35)):
                        text_lines = fallback_lines
                        source = "column_line_fallback"

                    # Quality guard: if DocAI region text does not align with this column's
                    # original line set, treat region OCR as unreliable and fall back.
                    if source == "docai_region":
                        ref_non_price = [ln for ln in fallback_lines if not _is_price_like_text(ln)]
                        ocr_non_price = [ln for ln in text_lines if not _is_price_like_text(ln)]
                        coverage = _column_text_coverage(ocr_non_price, ref_non_price)
                        if (
                            len(ref_non_price) >= 6
                            and (
                                coverage < 0.45
                                or len(ocr_non_price) < max(4, int(len(ref_non_price) * 0.35))
                            )
                        ):
                            text_lines = fallback_lines
                            source = "column_line_fallback_quality"
                    else:
                        coverage = None

                    parts.append(f"[COLUMN {col_no}]")
                    parts.extend(text_lines)
                    page_debug["columns"].append(
                        {
                            "column": col_no,
                            "source_column": source_col_no,
                            "bbox": [round(float(v), 1) for v in bbox[:4]],
                            "line_count": len(text_lines),
                            "source": source,
                            "coverage": round(float(coverage), 3) if coverage is not None else None,
                            "error": error_text,
                        }
                    )
                    if source == "docai_region":
                        used_region_ocr = True

                if footer_lines:
                    parts.append("[FOOTER]")
                    for line in processor._order_lines_for_seed(footer_lines):
                        text = _clean_text_line(line.get("text"))
                        if text:
                            parts.append(text)

                page_chunks.append("\n".join(parts).strip())
                debug_pages.append(page_debug)

    return "\n\n".join([chunk for chunk in page_chunks if chunk]).strip(), {"pages": debug_pages}, used_region_ocr


def run_menu_pipeline(
    pdf_path: Path,
    output_root: Path,
    dpi: int = 350,
    use_openai: bool = True,
    cleanup: bool = True,
    menu_name_hint: str | None = None,
) -> Dict:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    out_dir = create_output_dir(pdf_path, output_root)
    input_pdf = out_dir / "input.pdf"
    input_pdf.write_bytes(pdf_path.read_bytes())

    config = FullMenuConfig(dpi=dpi)
    processor = FullMenuOCR(config=config)

    # Google Document AI OCR (all page text/blocks) + CV icon detection
    docai = process_pdf_with_docai(input_pdf)
    (out_dir / "docai_raw.json").write_text(json.dumps(docai, indent=2), encoding="utf-8")
    docai_text_raw = processor.docai_raw_text(docai)
    pdf_text_raw = processor.pdf_raw_text(input_pdf)
    text_source = "docai"
    docai_score = processor._text_quality_score(docai_text_raw)
    pdf_score = processor._text_quality_score(pdf_text_raw) if pdf_text_raw else 0.0
    if pdf_text_raw and len(pdf_text_raw) >= 200 and pdf_score >= 0.6 and pdf_score >= (docai_score - 0.02):
        docai_text_raw = pdf_text_raw
        text_source = "pdf"
    (out_dir / "docai_text_raw.txt").write_text(docai_text_raw, encoding="utf-8")
    source_meta: Dict[str, Any] = {
        "source": text_source,
        "docai_score": docai_score,
        "pdf_score": pdf_score if pdf_text_raw else None,
    }
    (out_dir / "docai_text_source.json").write_text(json.dumps(source_meta, indent=2), encoding="utf-8")
    processor.process_docai_document(input_pdf, docai, out_dir, use_openai=False)

    menu_raw_path = out_dir / "menu_raw.json"
    menu_raw = json.loads(menu_raw_path.read_text(encoding="utf-8")) if menu_raw_path.exists() else None
    if menu_raw:
        menu_raw = processor.annotate_menu_raw_layout(menu_raw)
        menu_raw_path.write_text(json.dumps(menu_raw, indent=2), encoding="utf-8")

    formatted: Dict
    openai_status = None
    deterministic = processor.format_menu_deterministic(menu_raw) if menu_raw else {
        "menu_name": None,
        "items": [],
        "other_text": [],
        "footer_text": [],
        "notes": [],
    }
    hint = str(menu_name_hint or "").strip()
    if hint:
        deterministic["menu_name"] = Path(hint).stem
    row_column_details = None
    layout_lines = None
    layout_profile: Dict[str, Any] | None = None
    text_menu_raw = menu_raw
    docai_text = docai_text_raw
    final_text_source = text_source
    layout_detected = False
    use_layout = False
    simple_embedded_pdf_mode = False
    region_ocr_meta: Dict[str, Any] | None = None
    region_ocr_applied = False
    region_ocr_attempted = False
    if text_menu_raw:
        row_column_details = processor.build_row_column_details(text_menu_raw)
        layout_lines = processor.build_layout_lines(text_menu_raw)
        (out_dir / "layout_regions.json").write_text(
            json.dumps(row_column_details, indent=2),
            encoding="utf-8",
        )
        (out_dir / "layout_lines.json").write_text(
            json.dumps(layout_lines, indent=2),
            encoding="utf-8",
        )
        layout_profile = _summarize_layout_complexity(text_menu_raw, processor, text_source=text_source)
        (out_dir / "layout_complexity.json").write_text(
            json.dumps(layout_profile, indent=2),
            encoding="utf-8",
        )
        layout_detected = bool(layout_profile.get("has_multi_column_pages"))
        # For embedded-text PDFs, stay on native text only when layout is simple.
        simple_embedded_pdf_mode = bool(layout_profile.get("embedded_text_pdf")) and not layout_detected
        use_layout = bool(layout_profile.get("use_layout_path"))

        if use_layout:
            layout_candidate = processor.build_layout_aware_raw_text(
                text_menu_raw,
                fallback_raw_text=docai_text_raw,
            )
            if layout_candidate:
                docai_text = layout_candidate
                final_text_source = "layout_lines"
            region_ocr_attempted = True
            region_text, region_ocr_meta, used_region_ocr = _build_region_docai_raw_text(
                input_pdf=input_pdf,
                menu_raw=text_menu_raw,
                processor=processor,
                out_dir=out_dir,
                dpi=dpi,
            )
            (out_dir / "layout_region_ocr.json").write_text(
                json.dumps(region_ocr_meta, indent=2),
                encoding="utf-8",
            )
            if region_text:
                docai_text = region_text
                final_text_source = "docai_region" if used_region_ocr else "layout_region"
                region_ocr_applied = bool(used_region_ocr)
        else:
            docai_text = docai_text_raw
            final_text_source = text_source

    source_meta["source_final"] = final_text_source
    source_meta["layout_detected"] = bool(layout_detected)
    source_meta["layout_complex_path_used"] = bool(use_layout)
    source_meta["simple_embedded_pdf_mode"] = bool(simple_embedded_pdf_mode)
    source_meta["region_ocr_applied"] = bool(region_ocr_applied)
    source_meta["region_ocr_attempted"] = bool(region_ocr_attempted)
    if isinstance(layout_profile, dict):
        source_meta["layout_profile"] = layout_profile
    (out_dir / "docai_text_source.json").write_text(json.dumps(source_meta, indent=2), encoding="utf-8")
    (out_dir / "docai_text.txt").write_text(docai_text, encoding="utf-8")
    icon_lines = []
    icon_detections: List[Dict[str, Any]] = []
    if text_menu_raw:
        for page in text_menu_raw.get("pages", []):
            page_num = page.get("page")
            lines = page.get("lines", []) if isinstance(page, dict) else []
            ordered_lines = _order_lines(lines if isinstance(lines, list) else [])
            try:
                page_no_int = int(page_num)
            except Exception:
                page_no_int = 0
            for idx_line, line in enumerate(ordered_lines, start=1):
                raw_icons = line.get("icons") or []
                icons: List[str] = []
                for icon in raw_icons:
                    val = str(icon or "").strip().lower()
                    if val and val not in icons:
                        icons.append(val)
                if not icons:
                    continue
                col_idx = None
                if isinstance(line.get("column_index"), (int, float)):
                    col_idx = int(line.get("column_index"))
                role_val = str(line.get("layout_role") or "").strip().lower() or None
                line_id = f"p{page_no_int:03d}_l{idx_line:04d}"
                icon_lines.append(
                    {
                        "page": page_num,
                        "line_id": line_id,
                        "text": line.get("text"),
                        "bbox": line.get("bbox"),
                        "icons": icons,
                        "column_index": col_idx,
                        "layout_role": role_val,
                    }
                )
            page_icons = page.get("icons", []) if isinstance(page, dict) else []
            if isinstance(page_icons, list):
                for det in page_icons:
                    if not isinstance(det, dict):
                        continue
                    label = str(det.get("label") or "").strip().lower()
                    bbox = det.get("bbox")
                    if not label or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                        continue
                    icon_detections.append(
                        {
                            "page": page_num,
                            "label": label,
                            "bbox": [
                                _to_float(bbox[0]),
                                _to_float(bbox[1]),
                                _to_float(bbox[2]),
                                _to_float(bbox[3]),
                            ],
                            "score": float(det.get("score") or 0.0),
                            "line_index": (
                                int(det.get("line_index"))
                                if isinstance(det.get("line_index"), (int, float))
                                else None
                            ),
                        }
                    )
    docai_with_icons = {
        "raw_text": docai_text,
        "icon_lines": icon_lines,
        "icon_detections": icon_detections,
    }
    (out_dir / "docai_text_with_icons.json").write_text(
        json.dumps(docai_with_icons, indent=2),
        encoding="utf-8",
    )
    if use_openai:
        result = processor.format_raw_text_with_openai_result(
            raw_text=docai_text,
            deterministic=deterministic,
            row_column_details=row_column_details,
            layout_lines=layout_lines,
            icon_lines=icon_lines,
            icon_detections=icon_detections,
        )
        formatted = result.get("formatted") or {"error": "formatting failed"}
        openai_status = {
            "source": result.get("source"),
            "error": result.get("error"),
        }
        if result.get("openai_raw"):
            (out_dir / "openai_raw.txt").write_text(result["openai_raw"], encoding="utf-8")
        if result.get("openai_parsed"):
            (out_dir / "openai_parsed.json").write_text(
                json.dumps(result["openai_parsed"], indent=2), encoding="utf-8"
            )
    elif menu_raw:
        formatted = deterministic
        openai_status = {"source": "deterministic", "error": "OpenAI disabled"}
    else:
        formatted = deterministic
        openai_status = {"source": "deterministic", "error": "menu_raw missing"}

    (out_dir / "menu_formatted.json").write_text(json.dumps(formatted, indent=2), encoding="utf-8")
    if openai_status is not None:
        (out_dir / "openai_status.json").write_text(json.dumps(openai_status, indent=2), encoding="utf-8")
    copy_icon_details(out_dir)

    if cleanup:
        cleanup_output(out_dir)

    return {
        "output_dir": str(out_dir),
        "menu_raw": str(menu_raw_path) if menu_raw_path.exists() else None,
        "menu_formatted": str(out_dir / "menu_formatted.json"),
    }
