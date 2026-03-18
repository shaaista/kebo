from __future__ import annotations

import json
import random
import re
import os
import base64
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

try:
    import cv2  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("opencv-python is required") from exc

try:
    import fitz  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyMuPDF (fitz) is required") from exc

try:
    import pytesseract  # type: ignore
except Exception:  # pragma: no cover
    pytesseract = None

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, OpenAI

try:
    from .config import ALLOWED_LABELS, PHRASE_SYNONYMS, TOKEN_SYNONYMS, LegendConfig
    from .legend_extractor import LegendExtractor
except ImportError:  # fallback for direct script usage
    from config import ALLOWED_LABELS, PHRASE_SYNONYMS, TOKEN_SYNONYMS, LegendConfig
    from legend_extractor import LegendExtractor

Word = Tuple[float, float, float, float, str]
WordConf = Tuple[float, float, float, float, str, float]
Box = Tuple[float, float, float, float]


@dataclass(frozen=True)
class FullMenuConfig:
    dpi: int = 350
    legend_dpi: int = 800
    debug_icon_candidates: bool = True
    ocr_conf: int = 15
    footer_band_ratio: float = 0.25
    line_merge_tol: float = 0.6
    icon_strict: bool = False
    icon_match_threshold: float = 0.6
    icon_match_threshold_gray: float = 0.35
    icon_match_threshold_fallback: float = 0.7
    icon_match_threshold_gray_fallback: float = 0.6
    icon_iou_threshold: float = 0.3
    icon_scales: Tuple[float, ...] = (0.6, 0.8, 1.0, 1.2, 1.4, 1.6)
    max_detections_per_label: int = 20
    openai_timeout: int = 60
    openai_page_by_page: bool = True
    vision_layout_enabled: bool = True
    vision_layout_min_confidence: float = 0.45
    vision_layout_dpi: int = 170
    vision_layout_max_regions: int = 4
    line_icon_max_dx_factor: float = 8.0
    line_icon_y_factor: float = 1.0
    icon_candidate_min_area_ratio: float = 0.1
    icon_candidate_max_area_ratio: float = 2.5
    icon_candidate_aspect_min: float = 0.4
    icon_candidate_aspect_max: float = 2.8
    icon_candidate_pad_ratio: float = 0.25
    icon_line_pad_factor: float = 0.5
    icon_line_pad_factor_relaxed: float = 1.6
    icon_max_per_line: int = 5
    line_icon_left_dx_factor: float = 3.0
    line_icon_right_dx_factor: float = 6.0
    line_icon_right_dx_factor_kcal: float = 12.0
    line_icon_right_start_ratio: float = 0.55
    line_icon_inside_factor: float = 1.4
    icon_edge_allow_factor: float = 1.6
    line_icon_min_size_factor: float = 0.4
    line_icon_max_size_factor: float = 1.8
    icon_score_threshold: float = 0.37
    icon_score_threshold_veg: float = 0.33
    icon_relaxed_threshold_factor: float = 0.85
    icon_match_margin: float = 0.02
    icon_score_threshold_global: float = 0.5
    icon_match_margin_global: float = 0.08
    icon_min_similarity: float = 0.12
    icon_fg_ratio_min: float = 0.01
    icon_fg_ratio_max: float = 0.55
    icon_fg_ratio_tol: float = 2.0
    icon_max_components: int = 40
    icon_ssim_weight: float = 0.25
    icon_orb_weight: float = 0.2
    icon_shape_weight: float = 0.1
    icon_template_weight: float = 0.2
    icon_binary_iou_weight: float = 0.25
    icon_binary_iou_min: float = 0.22
    icon_binary_iou_strict: float = 0.6
    icon_shape_similarity_strict: float = 0.7
    icon_text_overlap_max: float = 0.6
    icon_text_overlap_score_boost: float = 0.08
    icon_text_overlap_margin_boost: float = 0.04
    icon_orb_features: int = 200
    icon_color_sat_min: float = 25.0
    icon_global_size_min_ratio: float = 0.55
    icon_global_size_max_ratio: float = 1.9
    icon_global_area_min_ratio: float = 0.15
    icon_global_area_max_ratio: float = 2.8
    icon_assign_max_dx_factor: float = 12.0
    icon_assign_y_factor: float = 0.5
    icon_scan_threshold: float = 0.45
    icon_scan_gray_threshold: float = 0.3
    icon_component_min_size_factor: float = 0.12
    icon_component_min_area_ratio_factor: float = 0.25
    icon_component_gap_factor: float = 0.2
    row_merge_tol: float = 0.8
    row_merge_x_overlap: float = 0.35
    row_merge_x_gap_factor: float = 2.5
    icon_global_scan_threshold: float = 0.38
    icon_global_scan_gray_threshold: float = 0.28
    icon_global_max_per_label: int = 60
    icon_full_scan: bool = False
    column_detect_enabled: bool = False
    column_gap_min_ratio: float = 0.08
    column_min_width_ratio: float = 0.28
    column_ink_threshold: float = 0.02
    column_ink_balance_ratio: float = 0.55
    column_price_strip_max_width_ratio: float = 0.38
    price_split_gap_ratio: float = 0.18
    price_token_ratio_threshold: float = 0.6
    column_word_min_count: int = 30
    column_word_balance_ratio: float = 0.35
    column_word_gap_min_ratio: float = 0.04
    enable_menu_specific_post_fixes: bool = False


class FullMenuOCR:
    def __init__(self, config: FullMenuConfig, tesseract_cmd: str | None = None) -> None:
        self.config = config
        self._tesseract_cmd = tesseract_cmd
        if pytesseract is not None and tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self._orb = cv2.ORB_create(nfeatures=self.config.icon_orb_features)

    def process_pdf(self, pdf_path: Path, output_dir: Path, use_openai: bool = True) -> Dict:
        if pytesseract is None:
            raise RuntimeError(
                "pytesseract is not installed. Use process_docai_json/process_docai_document for Google OCR flow."
            )
        pdf_path = Path(pdf_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Ensure legend icons exist (used as templates)
        icons_dir = output_dir / "legend_icons"
        if not icons_dir.exists() or not list(icons_dir.glob("*.png")):
            legend = LegendExtractor(
                LegendConfig(dpi=self.config.legend_dpi, tesseract_cmd=self._tesseract_cmd)
            )
            legend_results = legend.process_pdf(pdf_path, output_dir / "legend")
            (output_dir / "legend_summary.json").write_text(
                json.dumps(legend_results, indent=2), encoding="utf-8"
            )
            # copy icons from legend output
            src_icons = (output_dir / "legend" / "icons")
            icons_dir.mkdir(parents=True, exist_ok=True)
            for p in src_icons.glob("*.png"):
                target = icons_dir / p.name
                target.write_bytes(p.read_bytes())

        templates = self._load_templates(icons_dir)
        doc = fitz.open(pdf_path)

        raw_pages: List[Dict] = []
        structured_pages: List[Dict] = []

        for page_index in range(len(doc)):
            page = doc[page_index]
            out_raw = output_dir / f"page_{page_index + 1:02d}_raw.json"
            if out_raw.exists():
                page_data = json.loads(out_raw.read_text(encoding="utf-8"))
            else:
                image_rgb = self._render_page(page)
                footer_top_img = int(image_rgb.shape[0] * (1.0 - self.config.footer_band_ratio))

                lines = self._extract_lines(image_rgb)
                icons, lines = self._detect_icons_by_line(image_rgb, templates, lines, output_dir=output_dir)
                lines = self._annotate_layout_columns(lines)
                lines = self._propagate_icons_to_rows(lines)
                lines = self._filter_icons_to_dish_lines(lines)
                page_text = "\n".join(line["text"] for line in lines)

                page_data = {
                    "page": page_index + 1,
                    "lines": lines,
                    "icons": icons,
                    "page_text": page_text,
                }
                out_raw.write_text(json.dumps(page_data, indent=2), encoding="utf-8")

            if isinstance(page_data, dict):
                page_lines = page_data.get("lines", [])
                if isinstance(page_lines, list) and page_lines:
                    page_data["lines"] = self._annotate_layout_columns(page_lines)
            raw_pages.append(page_data)

            if use_openai:
                out_struct = output_dir / f"page_{page_index + 1:02d}_structured.json"
                if out_struct.exists():
                    structured = json.loads(out_struct.read_text(encoding="utf-8"))
                else:
                    structured = self._structure_with_openai(page_data)
                    out_struct.write_text(json.dumps(structured, indent=2), encoding="utf-8")
                structured_pages.append(structured)

        menu_raw = {
            "pdf": str(pdf_path),
            "pages": raw_pages,
        }
        (output_dir / "menu_raw.json").write_text(json.dumps(menu_raw, indent=2), encoding="utf-8")

        if use_openai:
            menu_struct = {
                "pdf": str(pdf_path),
                "pages": structured_pages,
            }
            (output_dir / "menu_structured.json").write_text(json.dumps(menu_struct, indent=2), encoding="utf-8")

        return {
            "raw_pages": raw_pages,
            "structured_pages": structured_pages,
        }

    def process_docai_json(
        self, pdf_path: Path, docai_json_path: Path, output_dir: Path, use_openai: bool = True
    ) -> Dict:
        pdf_path = Path(pdf_path)
        docai_json_path = Path(docai_json_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not docai_json_path.exists():
            raise FileNotFoundError(f"DocAI JSON not found: {docai_json_path}")

        docai = json.loads(docai_json_path.read_text(encoding="utf-8"))
        return self.process_docai_document(pdf_path, docai, output_dir, use_openai=use_openai)

    def process_docai_document(
        self, pdf_path: Path, docai: Dict[str, Any], output_dir: Path, use_openai: bool = True
    ) -> Dict:
        pdf_path = Path(pdf_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        docai_doc = self._docai_get_document(docai)
        docai_pages = docai_doc.get("pages", []) if isinstance(docai_doc, dict) else []

        # Ensure legend icons exist (used as templates)
        icons_dir = output_dir / "legend_icons"
        if not icons_dir.exists() or not list(icons_dir.glob("*.png")):
            legend = LegendExtractor(
                LegendConfig(dpi=self.config.legend_dpi, tesseract_cmd=self._tesseract_cmd)
            )
            legend_results = legend.process_pdf(pdf_path, output_dir / "legend")
            (output_dir / "legend_summary.json").write_text(
                json.dumps(legend_results, indent=2), encoding="utf-8"
            )
            src_icons = output_dir / "legend" / "icons"
            icons_dir.mkdir(parents=True, exist_ok=True)
            for p in src_icons.glob("*.png"):
                target = icons_dir / p.name
                target.write_bytes(p.read_bytes())

        templates = self._load_templates(icons_dir)
        doc = fitz.open(pdf_path)

        raw_pages: List[Dict] = []
        structured_pages: List[Dict] = []

        for page_index in range(len(doc)):
            page = doc[page_index]
            out_raw = output_dir / f"page_{page_index + 1:02d}_raw.json"
            if out_raw.exists():
                page_data = json.loads(out_raw.read_text(encoding="utf-8"))
            else:
                image_rgb = self._render_page(page)
                lines = self._docai_extract_lines(
                    docai_doc=docai_doc,
                    docai_pages=docai_pages,
                    page_index=page_index,
                    image_shape=image_rgb.shape,
                )
                blocks = self._docai_extract_blocks(
                    docai_doc=docai_doc,
                    docai_pages=docai_pages,
                    page_index=page_index,
                    image_shape=image_rgb.shape,
                )
                icons, lines = self._detect_icons_by_line(image_rgb, templates, lines, output_dir=output_dir)
                lines = self._annotate_layout_columns(lines)
                lines = self._propagate_icons_to_rows(lines)
                lines = self._merge_icons_to_prev_line(lines, only_labels={"veg", "non_veg"})
                lines = self._merge_icons_to_prev_line(lines, only_labels={"veg", "non_veg"})
                lines = self._filter_icons_to_dish_lines(lines)
                page_text = "\n".join(line.get("text", "") for line in lines)
                page_data = {
                    "page": page_index + 1,
                    "lines": lines,
                    "blocks": blocks,
                    "icons": icons,
                    "page_text": page_text,
                }
                out_raw.write_text(json.dumps(page_data, indent=2), encoding="utf-8")

            if isinstance(page_data, dict):
                page_lines = page_data.get("lines", [])
                if isinstance(page_lines, list) and page_lines:
                    page_data["lines"] = self._annotate_layout_columns(page_lines)
            raw_pages.append(page_data)

            if use_openai:
                out_struct = output_dir / f"page_{page_index + 1:02d}_structured.json"
                if out_struct.exists():
                    structured = json.loads(out_struct.read_text(encoding="utf-8"))
                else:
                    structured = self._structure_with_openai(page_data)
                    out_struct.write_text(json.dumps(structured, indent=2), encoding="utf-8")
                structured_pages.append(structured)

        menu_raw = {"pdf": str(pdf_path), "pages": raw_pages}
        (output_dir / "menu_raw.json").write_text(json.dumps(menu_raw, indent=2), encoding="utf-8")

        if use_openai:
            menu_struct = {"pdf": str(pdf_path), "pages": structured_pages}
            (output_dir / "menu_structured.json").write_text(json.dumps(menu_struct, indent=2), encoding="utf-8")

        return {"raw_pages": raw_pages, "structured_pages": structured_pages}

    def structure_existing(self, output_dir: Path, force: bool = False) -> List[Dict]:
        output_dir = Path(output_dir)
        structured_pages: List[Dict] = []
        raw_files = sorted(output_dir.glob("page_*_raw.json"))
        for raw_path in raw_files:
            page_data = json.loads(raw_path.read_text(encoding="utf-8"))
            struct_path = raw_path.with_name(raw_path.name.replace("_raw.json", "_structured.json"))
            if struct_path.exists() and not force:
                structured = json.loads(struct_path.read_text(encoding="utf-8"))
                if isinstance(structured, dict) and "raw" in structured and isinstance(structured["raw"], str):
                    parsed = self._parse_json_maybe(structured["raw"])
                    if parsed is not None:
                        structured = parsed
                        struct_path.write_text(json.dumps(structured, indent=2), encoding="utf-8")
                if isinstance(structured, dict) and structured.get("error"):
                    structured = self._structure_with_openai(page_data)
                    struct_path.write_text(json.dumps(structured, indent=2), encoding="utf-8")
            else:
                structured = self._structure_with_openai(page_data)
                struct_path.write_text(json.dumps(structured, indent=2), encoding="utf-8")

            if isinstance(structured, dict):
                structured.setdefault("all_lines", page_data.get("lines", []))
                if "page" not in structured:
                    structured["page"] = page_data.get("page")
                struct_path.write_text(json.dumps(structured, indent=2), encoding="utf-8")
            structured_pages.append(structured)

        menu_struct = {"pdf": None, "pages": structured_pages}
        (output_dir / "menu_structured.json").write_text(json.dumps(menu_struct, indent=2), encoding="utf-8")
        return structured_pages

    def reattach_icons_existing(self, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        raw_files = sorted(output_dir.glob("page_*_raw.json"))
        for raw_path in raw_files:
            page_data = json.loads(raw_path.read_text(encoding="utf-8"))
            lines = page_data.get("lines", [])
            icons = page_data.get("icons", [])
            if not lines or not icons:
                continue
            lines = self._attach_icons_to_lines(lines, icons)
            page_data["lines"] = lines
            page_data["page_text"] = "\n".join(line.get("text", "") for line in lines)
            raw_path.write_text(json.dumps(page_data, indent=2), encoding="utf-8")

    def redetect_icons_existing(
        self, pdf_path: Path, output_dir: Path, page_from: int | None = None, page_to: int | None = None
    ) -> None:
        output_dir = Path(output_dir)
        templates = self._load_templates(output_dir / "legend_icons")
        doc = fitz.open(pdf_path)

        start_idx = 0 if page_from is None else max(0, page_from - 1)
        end_idx = (len(doc) - 1) if page_to is None else min(len(doc) - 1, page_to - 1)

        for page_index in range(start_idx, end_idx + 1):
            out_raw = output_dir / f"page_{page_index + 1:02d}_raw.json"
            if not out_raw.exists():
                continue
            page_data = json.loads(out_raw.read_text(encoding="utf-8"))
            lines = page_data.get("lines", [])
            if not lines:
                continue
            page = doc[page_index]
            image_rgb = self._render_page(page)
            icons, lines = self._detect_icons_by_line(image_rgb, templates, lines, output_dir=output_dir)
            lines = self._propagate_icons_to_rows(lines)
            lines = self._filter_icons_to_dish_lines(lines)
            page_data["lines"] = lines
            page_data["icons"] = icons
            page_data["page_text"] = "\n".join(line.get("text", "") for line in lines)
            out_raw.write_text(json.dumps(page_data, indent=2), encoding="utf-8")

    def _render_page(self, page) -> np.ndarray:
        pix = page.get_pixmap(dpi=self.config.dpi, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        return img

    def _extract_lines(self, image_rgb: np.ndarray) -> List[Dict]:
        if image_rgb.size == 0:
            return []

        h, w = image_rgb.shape[:2]
        columns = [(0, w)]

        all_lines: List[Dict] = []
        for _, (x0, x1) in enumerate(columns):
            if x1 <= x0:
                continue
            crop = image_rgb[:, int(x0) : int(x1)]
            words = self._extract_words_multi(crop, x_offset=float(x0), y_offset=0.0)
            if not words:
                continue
            all_lines.extend(
                self._group_words_into_lines(
                    words,
                    page_width=float(w),
                    column_index=None,
                    column_bounds=None,
                )
            )

        return all_lines

    def _extract_words_multi(self, image_rgb: np.ndarray, x_offset: float = 0.0, y_offset: float = 0.0) -> List[Word]:
        words: List[WordConf] = []
        # original
        words.extend(self._tesseract_words(image_rgb, config="--oem 3 --psm 6"))
        words.extend(self._tesseract_words(image_rgb, config="--oem 3 --psm 4"))

        # preprocessed (sharpen + adaptive threshold)
        processed = self._preprocess_for_ocr(image_rgb)
        words.extend(self._tesseract_words(processed, config="--oem 3 --psm 6"))
        words.extend(self._tesseract_words(processed, config="--oem 3 --psm 4"))

        merged = self._merge_words(words)
        adjusted: List[Word] = []
        for w in merged:
            x0, y0, x1, y1, text, _ = w
            adjusted.append((x0 + x_offset, y0 + y_offset, x1 + x_offset, y1 + y_offset, text))
        return adjusted

    def _group_words_into_lines(
        self,
        words: List[Word],
        page_width: float | None = None,
        column_index: int | None = None,
        column_bounds: Tuple[int, int] | None = None,
    ) -> List[Dict]:
        if not words:
            return []

        heights = [abs(w[3] - w[1]) for w in words]
        median_h = float(np.median(heights)) if heights else 10.0
        tol = max(2.0, median_h * self.config.line_merge_tol)

        sorted_words = sorted(words, key=lambda w: (w[1], w[0]))
        lines: List[List[Word]] = []
        line_y: List[float] = []

        for word in sorted_words:
            y_center = (word[1] + word[3]) / 2.0
            if not lines or abs(y_center - line_y[-1]) > tol:
                lines.append([word])
                line_y.append(y_center)
            else:
                lines[-1].append(word)
                line_y[-1] = (line_y[-1] + y_center) / 2.0

        results: List[Dict] = []
        for line in lines:
            line.sort(key=lambda w: w[0])
            text = " ".join(w[4] for w in line)
            x0 = min(w[0] for w in line)
            y0 = min(w[1] for w in line)
            x1 = max(w[2] for w in line)
            y1 = max(w[3] for w in line)
            line_dict = {"text": text, "bbox": [x0, y0, x1, y1], "icons": []}
            if column_index is not None:
                line_dict["column_index"] = column_index
            if column_bounds is not None:
                line_dict["column_bbox"] = [float(column_bounds[0]), float(column_bounds[1])]
            if page_width:
                fields = self._split_line_fields(line, page_width)
                if fields:
                    line_dict.update(fields)
            results.append(line_dict)

        return results

    def _detect_columns(self, image_rgb: np.ndarray) -> List[Tuple[int, int]]:
        h, w = image_rgb.shape[:2]
        if w < 200:
            return [(0, w)]
        gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
        try:
            bw = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 11
            )
        except Exception:
            return [(0, w)]

        ink = (bw > 0).sum(axis=0).astype(np.float32)
        ink_ratio = ink / float(h)
        gap = ink_ratio < self.config.column_ink_threshold

        # find all significant gaps
        gaps: List[Tuple[int, int]] = []
        start = None
        for i, is_gap in enumerate(gap):
            if is_gap and start is None:
                start = i
            if not is_gap and start is not None:
                end = i
                if (end - start) >= int(w * self.config.column_gap_min_ratio):
                    gaps.append((start, end))
                start = None
        if start is not None:
            end = w
            if (end - start) >= int(w * self.config.column_gap_min_ratio):
                gaps.append((start, end))

        if not gaps:
            return self._detect_columns_by_words(image_rgb) or [(0, w)]

        # ignore gaps at extreme edges (page margins)
        edge_pad = int(w * 0.05)
        gaps = [g for g in gaps if g[0] > edge_pad and g[1] < (w - edge_pad)]
        if not gaps:
            return self._detect_columns_by_words(image_rgb) or [(0, w)]

        # build columns from gaps
        gaps = sorted(gaps, key=lambda g: g[0])
        columns: List[Tuple[int, int]] = []
        prev = 0
        for gs, ge in gaps:
            if gs > prev:
                columns.append((prev, gs))
            prev = ge
        if prev < w:
            columns.append((prev, w))

        # validate widths
        min_w = int(w * self.config.column_min_width_ratio)
        if any((c[1] - c[0]) < min_w for c in columns):
            return [(0, w)]

        # price-strip guard: if rightmost column is narrow + low ink, treat as single column
        if len(columns) == 2:
            left, right = columns
            left_w = left[1] - left[0]
            right_w = right[1] - right[0]
            left_ink = self._column_ink_ratio(bw, left[0], left[1])
            right_ink = self._column_ink_ratio(bw, right[0], right[1])
            if (
                right_w < int(w * self.config.column_price_strip_max_width_ratio)
                and right_ink < (left_ink * self.config.column_ink_balance_ratio)
            ):
                return self._detect_columns_by_words(image_rgb) or [(0, w)]

        return columns

    def _detect_columns_by_words(self, image_rgb: np.ndarray) -> List[Tuple[int, int]] | None:
        if pytesseract is None:
            return None
        h, w = image_rgb.shape[:2]
        if w < 200:
            return None
        # lightweight OCR pass for word boxes
        data = pytesseract.image_to_data(
            image_rgb, output_type=pytesseract.Output.DICT, config="--oem 3 --psm 6"
        )
        words = []
        for text, conf, left, top, width, height in zip(
            data["text"], data["conf"], data["left"], data["top"], data["width"], data["height"]
        ):
            if not text or not text.strip():
                continue
            try:
                c = float(conf)
            except Exception:
                c = -1.0
            if c < self.config.ocr_conf:
                continue
            x0 = float(left)
            x1 = float(left + width)
            words.append((x0, x1, text.strip()))

        if len(words) < self.config.column_word_min_count:
            return None

        xs = [(w[0] + w[1]) / 2.0 for w in words]
        c1, c2 = w * 0.33, w * 0.66
        for _ in range(8):
            left = [x for x in xs if abs(x - c1) <= abs(x - c2)]
            right = [x for x in xs if abs(x - c2) < abs(x - c1)]
            if left:
                c1 = sum(left) / len(left)
            if right:
                c2 = sum(right) / len(right)

        if c1 > c2:
            c1, c2 = c2, c1

        left_words = [w for w in words if (w[0] + w[1]) / 2.0 <= (c1 + c2) / 2.0]
        right_words = [w for w in words if (w[0] + w[1]) / 2.0 > (c1 + c2) / 2.0]

        if not left_words or not right_words:
            return None

        balance = min(len(left_words), len(right_words)) / max(len(left_words), len(right_words))
        if balance < self.config.column_word_balance_ratio:
            return None

        left_x0 = min(wd[0] for wd in left_words)
        left_x1 = max(wd[1] for wd in left_words)
        right_x0 = min(wd[0] for wd in right_words)
        right_x1 = max(wd[1] for wd in right_words)
        gap = right_x0 - left_x1
        if gap < (w * self.config.column_word_gap_min_ratio):
            return None

        # price-strip guard using numeric ratio on right column
        right_tokens = [t for _, _, t in right_words]
        price_like = 0
        for t in right_tokens:
            tl = t.lower()
            if any(sym in tl for sym in ("â‚¹", "inr", "rs", "aed", "sar", "$")):
                price_like += 1
            elif tl.replace(".", "").replace(",", "").isdigit():
                price_like += 1
        price_ratio = price_like / max(len(right_tokens), 1)
        right_w = right_x1 - right_x0
        if (
            right_w < int(w * self.config.column_price_strip_max_width_ratio)
            and price_ratio >= self.config.price_token_ratio_threshold
        ):
            return None

        return [(0, int(right_x0)), (int(right_x0), w)]

    def _column_ink_ratio(self, bw: np.ndarray, x0: int, x1: int) -> float:
        if x1 <= x0:
            return 0.0
        region = bw[:, x0:x1]
        if region.size == 0:
            return 0.0
        return float((region > 0).sum()) / float(region.size)

    def _split_line_fields(self, words: List[Word], page_width: float) -> Dict[str, str] | None:
        if not words:
            return None
        # identify potential price/kcal tokens
        tokens = [(w[4], w[0], w[2]) for w in words]
        price_like = []
        for text, x0, x1 in tokens:
            t = text.lower()
            if any(sym in t for sym in ("â‚¹", "inr", "rs", "aed", "sar", "$")):
                price_like.append((text, x0, x1))
                continue
            if t.replace(".", "").replace(",", "").isdigit() and len(t) <= 6:
                price_like.append((text, x0, x1))

        # find biggest x-gap
        words_sorted = sorted(words, key=lambda w: w[0])
        gaps = []
        for i in range(len(words_sorted) - 1):
            gap = words_sorted[i + 1][0] - words_sorted[i][2]
            gaps.append((gap, i))
        split_idx = None
        if gaps:
            gap, idx = max(gaps, key=lambda g: g[0])
            if gap > (page_width * self.config.price_split_gap_ratio):
                split_idx = idx

        # if price-like tokens are on the far right, split there
        if price_like and split_idx is None:
            rightmost_price = max(price_like, key=lambda p: p[2])
            for i, w in enumerate(words_sorted):
                if w[2] >= rightmost_price[2]:
                    split_idx = max(0, i - 1)
                    break

        if split_idx is None:
            return None

        left_words = words_sorted[: split_idx + 1]
        right_words = words_sorted[split_idx + 1 :]
        if not right_words:
            return None

        right_tokens = [w[4] for w in right_words]
        price_ratio = 0.0
        if right_tokens:
            price_like_right = 0
            for t in right_tokens:
                tl = t.lower()
                if any(sym in tl for sym in ("â‚¹", "inr", "rs", "aed", "sar", "$")):
                    price_like_right += 1
                elif tl.replace(".", "").replace(",", "").isdigit():
                    price_like_right += 1
            price_ratio = price_like_right / max(len(right_tokens), 1)

        if price_ratio < self.config.price_token_ratio_threshold:
            return None

        name_text = " ".join(w[4] for w in left_words)
        right_text = " ".join(w[4] for w in right_words)
        kcal_text = ""
        price_text = right_text
        if "kcal" in right_text.lower() or "kcai" in right_text.lower():
            kcal_text = right_text
        return {
            "name_text": name_text,
            "price_text": price_text,
            "kcal_text": kcal_text,
        }

    def _tesseract_words(self, image: np.ndarray, config: str) -> List[WordConf]:
        if pytesseract is None:
            return []
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config=config)
        results: List[WordConf] = []
        for text, conf, left, top, width, height in zip(
            data["text"], data["conf"], data["left"], data["top"], data["width"], data["height"]
        ):
            if not text or not text.strip():
                continue
            try:
                c = float(conf)
            except Exception:
                c = -1.0
            if c < self.config.ocr_conf:
                continue
            x0 = float(left)
            y0 = float(top)
            x1 = float(left + width)
            y1 = float(top + height)
            results.append((x0, y0, x1, y1, text.strip(), c))
        return results

    def _preprocess_for_ocr(self, image_rgb: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        sharp = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)
        thresh = cv2.adaptiveThreshold(
            sharp, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
        )
        return thresh

    def _merge_words(self, words: List[WordConf]) -> List[WordConf]:
        merged: Dict[Tuple[int, int, int, int], WordConf] = {}
        for w in words:
            x0, y0, x1, y1, text, conf = w
            cx = int((x0 + x1) / 2.0)
            cy = int((y0 + y1) / 2.0)
            key = (cx // 3, cy // 3, int((x1 - x0) // 3), int((y1 - y0) // 3))
            prev = merged.get(key)
            if prev is None or conf > prev[5]:
                merged[key] = w
        return list(merged.values())

    def _merge_icons_to_prev_line(self, lines: List[Dict], only_labels: set[str] | None = None) -> List[Dict]:
        """Attach icons from a line to the previous line (optionally filter by label set)."""
        if not lines:
            return lines
        for idx in range(1, len(lines)):
            cur_icons = lines[idx].get("icons") or []
            if only_labels is not None:
                # Avoid propagating from true name lines.
                if self._is_name_line(lines[idx]):
                    continue
                cur_icons = [icon for icon in cur_icons if icon in only_labels]
            if not cur_icons:
                continue
            target_idx = idx - 1
            if only_labels is not None:
                # Prefer the nearest previous name line to avoid cascading into prices/kcal.
                target_idx = None
                for j in range(idx - 1, max(-1, idx - 4), -1):
                    if self._is_name_line(lines[j]):
                        target_idx = j
                        break
                if target_idx is None:
                    continue
            prev_icons = lines[target_idx].get("icons") or []
            merged = list({*prev_icons, *cur_icons})
            lines[target_idx]["icons"] = merged
        return lines

    def _load_templates(self, icons_dir: Path) -> Dict[str, Dict[str, np.ndarray]]:
        templates: Dict[str, Dict[str, np.ndarray]] = {}
        for icon in icons_dir.glob("*.png"):
            img = cv2.imread(str(icon), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            # trim whitespace around icon
            _, bw = cv2.threshold(img, 245, 255, cv2.THRESH_BINARY_INV)
            ys, xs = np.where(bw > 0)
            if ys.size > 0 and xs.size > 0:
                x0, x1 = xs.min(), xs.max()
                y0, y1 = ys.min(), ys.max()
                img = img[y0 : y1 + 1, x0 : x1 + 1]
            _, bw_icon = cv2.threshold(img, 200, 255, cv2.THRESH_BINARY_INV)
            fg_ratio = float(np.count_nonzero(bw_icon)) / float(bw_icon.size or 1)
            edge = cv2.Canny(img, 50, 150)
            kp, des = self._orb.detectAndCompute(img, None)
            contour = self._largest_contour(edge)
            _, bw_template = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            # Tighten masks to reduce whitespace impact during IoU/SSIM matching.
            bw_mask = self._prepare_binary_mask(bw_template)
            bw_trim = None
            gray_trim = None
            fg_ratio_trim = fg_ratio
            if bw_mask is not None:
                ys, xs = np.where(bw_mask > 0)
                if ys.size > 0 and xs.size > 0:
                    x0, x1 = xs.min(), xs.max()
                    y0, y1 = ys.min(), ys.max()
                    bw_trim = bw_mask[y0 : y1 + 1, x0 : x1 + 1]
                    gray_trim = img[y0 : y1 + 1, x0 : x1 + 1]
                    fg_ratio_trim = float(np.count_nonzero(bw_trim)) / float(bw_trim.size or 1)
            templates[icon.stem] = {
                "gray": img,
                "gray_trim": gray_trim if gray_trim is not None else img,
                "edge": edge,
                "kp": kp,
                "des": des,
                "contour": contour,
                "fg_ratio": fg_ratio,
                "fg_ratio_trim": fg_ratio_trim,
                "bw": bw_template,
                "bw_trim": bw_trim if bw_trim is not None else bw_template,
            }
        return templates

    def _detect_icons_by_line(
        self,
        image_rgb: np.ndarray,
        templates: Dict[str, Dict[str, np.ndarray]],
        lines: List[Dict],
        output_dir: Path | None = None,
    ) -> Tuple[List[Dict], List[Dict]]:
        if not lines or not templates:
            return [], lines

        # reset any prior icon annotations
        for line in lines:
            line["icons"] = []
            line.pop("_icon_scores", None)

        h, w, _ = image_rgb.shape
        page_gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
        morph_lines = self._detect_text_lines_morphology(page_gray)
        line_morph_map = self._map_ocr_lines_to_morph(lines, morph_lines)
        detections: List[Dict] = []
        footer_top = self._infer_footer_top(lines, h)
        price_col_x = self._infer_price_column_x(lines, footer_top)
        for idx, line in enumerate(lines):
            lx0, ly0, lx1, ly1 = line["bbox"]
            is_footer_content = (
                "kcal" in (line.get("text") or "").lower() or 
                self._extract_price_from_line(line) is not None
            )
            if ly0 >= footer_top and not is_footer_content:
                continue
            lh = max(1.0, ly1 - ly0)
            lw = max(1.0, lx1 - lx0)
            # Ensure minimum band for thin lines (Panna Cotta fix)
            pad_h = max(lh * self.config.icon_line_pad_factor, 60.0)
            band_y0 = int(max(0, ly0 - pad_h))
            band_y1 = int(min(h, ly1 + pad_h))

            min_gap = int(max(2.0, lh * 0.1))

            left_x1 = int(max(0, lx0 - min_gap))
            left_x0 = int(max(0, left_x1 - (lh * self.config.line_icon_left_dx_factor)))

            is_kcal = self._extract_kcal_from_line(line) is not None
            right_dx = (
                self.config.line_icon_right_dx_factor_kcal
                if is_kcal
                else self.config.line_icon_right_dx_factor
            )
            right_x0 = int(min(w, lx1 + min_gap))
            right_x1 = int(min(w, right_x0 + (lh * right_dx)))
            price_limit = None
            if price_col_x is not None:
                price_limit = int(price_col_x - max(2.0, lh * 0.2))
                if price_limit <= right_x0:
                    price_limit = None
                else:
                    right_x1 = min(right_x1, price_limit)

            roi_list: List[Tuple[int, int, bool]] = []
            if left_x1 > left_x0:
                roi_list.append((left_x0, left_x1, False))
            inside_pad = int(max(2.0, lh * self.config.line_icon_inside_factor))
            left_inside_x0 = int(max(0, lx0 - min_gap))
            left_inside_x1 = int(min(w, lx0 + inside_pad))
            if left_inside_x1 > left_inside_x0:
                roi_list.append((left_inside_x0, left_inside_x1, True))
            # internal right-half region (icons often sit between name and price)
            inner_x0 = int(lx0 + (lw * self.config.line_icon_right_start_ratio))
            inner_x1 = int(min(w, lx1 + (lh * right_dx)))
            if price_limit is not None:
                inner_x1 = min(inner_x1, price_limit)
            if inner_x1 > inner_x0:
                roi_list.append((inner_x0, inner_x1, True))
            right_inside_x0 = int(max(0, lx1 - inside_pad))
            right_inside_x1 = int(min(w, lx1 + min_gap))
            if right_inside_x1 > right_inside_x0:
                roi_list.append((right_inside_x0, right_inside_x1, True))
            if right_x1 > right_x0:
                roi_list.append((right_x0, right_x1, False))

            if not roi_list or band_y1 <= band_y0:
                continue

            candidates = self._extract_icon_candidates(
                page_gray,
                band_y0,
                band_y1,
                roi_list,
                lh,
                line["bbox"],
            )
            if not candidates:
                continue

            line_dets: List[Dict] = []
            for cand in candidates:
                cx0, cy0, cx1, cy1 = cand
                crop = page_gray[int(cy0):int(cy1), int(cx0):int(cx1)]
                if crop.size == 0:
                    continue
                # Prefer color-based veg/non-veg classification for colored square icons.
                label = None
                score = 0.0
                margin = 0.0
                matched_by_color = False
                crop_rgb = image_rgb[int(cy0):int(cy1), int(cx0):int(cx1)]
                if crop_rgb.size > 0:
                    veg_label, veg_score = self._classify_veg_nonveg(crop_rgb, relaxed=not self.config.icon_strict)
                    if veg_label:
                        label, score, margin = veg_label, veg_score, veg_score
                        matched_by_color = True
                if label is None:
                    disallow = None
                    if crop_rgb.size > 0 and not self._is_color_candidate(crop_rgb):
                        disallow = {"veg", "non_veg"}
                    label, score, margin = self._match_icon_candidate(
                        crop, templates, disallow_labels=disallow
                    )
                if label is None:
                    continue
                threshold = (
                    self.config.icon_score_threshold_veg
                    if label in {"veg", "non_veg"}
                    else self.config.icon_score_threshold
                )
                overlap_ratio = self._overlap_ratio([cx0, cy0, cx1, cy1], line["bbox"])
                if overlap_ratio > self.config.icon_text_overlap_max and not matched_by_color:
                    threshold = max(threshold, self.config.icon_score_threshold + self.config.icon_text_overlap_score_boost)
                    if margin < (self.config.icon_match_margin + self.config.icon_text_overlap_margin_boost):
                        continue
                if score < threshold:
                    continue
                line_dets.append({"label": label, "bbox": [cx0, cy0, cx1, cy1], "score": score})

            if line_dets:
                # NMS + per-line selection
                line_dets = self._nms(line_dets, self.config.icon_iou_threshold)
                by_label: Dict[str, Dict] = {}
                for det in line_dets:
                    prev = by_label.get(det["label"])
                    if prev is None or det["score"] > prev["score"]:
                        by_label[det["label"]] = det

                veg_labels = {"veg", "non_veg"}
                selected: List[Tuple[str, float, List[float]]] = []
                for v in veg_labels:
                    if v in by_label:
                        d = by_label[v]
                        selected.append((v, d["score"], d["bbox"]))

                # If both veg and non_veg are present, keep the higher score only
                if len(selected) == 2:
                    selected = sorted(selected, key=lambda x: x[1], reverse=True)[:1]

                other_labels = [
                    (label, det["score"], det["bbox"])
                    for label, det in by_label.items()
                    if label not in veg_labels
                ]
                other_labels = sorted(other_labels, key=lambda x: x[1], reverse=True)[: self.config.icon_max_per_line]
                selected.extend(other_labels)

                for label, score, bbox in selected:
                    lines[idx]["icons"].append(label)
                    detections.append({"label": label, "bbox": bbox, "score": score, "line_index": idx})

                if lines[idx]["icons"]:
                    lines[idx]["icons"] = sorted(set(lines[idx]["icons"]))

            if len(lines[idx]["icons"]) < self.config.icon_max_per_line and self._looks_like_dish_line(
                line.get("text", "")
            ):
                # Fallback: dense template scan across the full line band (dish lines only)
                fallback = self._scan_line_band_for_icons(
                    page_gray=page_gray,
                    band_y0=band_y0,
                    band_y1=band_y1,
                    templates=templates,
                    page_rgb=image_rgb,
                )
                for det in fallback:
                    if det["label"] in lines[idx]["icons"]:
                        continue
                    lines[idx]["icons"].append(det["label"])
                    det["line_index"] = idx
                    detections.append(det)
                    if len(lines[idx]["icons"]) >= self.config.icon_max_per_line:
                        break
                if lines[idx]["icons"]:
                    lines[idx]["icons"] = sorted(set(lines[idx]["icons"]))

        # Global pass to catch icons outside narrow line ROIs
        global_dets = self._detect_icons_global(
            page_gray=page_gray,
            page_rgb=image_rgb,
            templates=templates,
            lines=lines,
            footer_top=footer_top,
            morph_lines=morph_lines,
            line_morph_map=line_morph_map,
        )
        if global_dets:
            for det in global_dets:
                line_index = det.get("line_index")
                if line_index is None:
                    continue
                if 0 <= line_index < len(lines):
                    lines[line_index]["icons"].append(det["label"])
                    detections.append(det)
            for line in lines:
                if line["icons"]:
                    line["icons"] = sorted(set(line["icons"]))

        # store best scores per line for conflict resolution
        line_scores: List[Dict[str, float]] = [dict() for _ in lines]
        for det in detections:
            idx = det.get("line_index")
            if idx is None or idx < 0 or idx >= len(lines):
                continue
            label = det.get("label")
            score = float(det.get("score", 0.0))
            if label:
                prev = line_scores[idx].get(label, -1.0)
                if score > prev:
                    line_scores[idx][label] = score
        for idx, line in enumerate(lines):
            if line_scores[idx]:
                line["_icon_scores"] = line_scores[idx]

        if self.config.debug_icon_candidates and output_dir is not None:
            try:
                debug_dir = Path(output_dir) / "debug_icons"
                debug_dir.mkdir(parents=True, exist_ok=True)
                sample_lines = [i for i, l in enumerate(lines) if self._looks_like_dish_line(l.get("text", ""))]
                sample_lines = sample_lines[:12]
                for i in sample_lines:
                    lx0, ly0, lx1, ly1 = lines[i]["bbox"]
                    pad = int(max(10.0, (ly1 - ly0) * 2.0))
                    x0 = max(0, int(lx0 - pad))
                    x1 = min(w, int(lx1 + pad))
                    y0 = max(0, int(ly0 - pad))
                    y1 = min(h, int(ly1 + pad))
                    crop = image_rgb[y0:y1, x0:x1]
                    if crop.size == 0:
                        continue
                    from PIL import Image

                    Image.fromarray(crop).save(debug_dir / f"line_{i:02d}.png")
            except Exception:
                pass

        return detections, lines

    def _scan_line_band_for_icons(
        self,
        page_gray: np.ndarray,
        band_y0: int,
        band_y1: int,
        templates: Dict[str, Dict[str, np.ndarray]],
        page_rgb: np.ndarray | None = None,
    ) -> List[Dict]:
        if band_y1 <= band_y0:
            return []
        band_gray = page_gray[band_y0:band_y1, :]
        if band_gray.size == 0:
            return []
        band_edge = cv2.Canny(band_gray, 50, 150)
        detections: List[Dict] = []
        for label, tmpl in templates.items():
            tmpl_gray = tmpl.get("gray")
            tmpl_edge = tmpl.get("edge")
            if tmpl_gray is None or tmpl_edge is None:
                continue
            th, tw = tmpl_edge.shape[:2]
            best_score = -1.0
            best_bbox = None
            for scale in self.config.icon_scales:
                rw = int(tw * scale)
                rh = int(th * scale)
                if rw < 6 or rh < 6:
                    continue
                if rw >= band_edge.shape[1] or rh >= band_edge.shape[0]:
                    continue
                resized_edge = cv2.resize(tmpl_edge, (rw, rh), interpolation=cv2.INTER_AREA)
                resized_gray = cv2.resize(tmpl_gray, (rw, rh), interpolation=cv2.INTER_AREA)
                res_edge = cv2.matchTemplate(band_edge, resized_edge, cv2.TM_CCOEFF_NORMED)
                _, max_edge, _, max_loc = cv2.minMaxLoc(res_edge)
                res_gray = cv2.matchTemplate(band_gray, resized_gray, cv2.TM_CCOEFF_NORMED)
                _, max_gray, _, _ = cv2.minMaxLoc(res_gray)
                score = (float(max_edge) + float(max_gray)) / 2.0
                if score < self.config.icon_scan_threshold:
                    continue
                if min(float(max_edge), float(max_gray)) < self.config.icon_scan_gray_threshold:
                    continue
                if score > best_score:
                    best_score = score
                    bx, by = max_loc
                    best_bbox = [
                        float(bx),
                        float(by),
                        float(bx + rw),
                        float(by + rh),
                    ]
            if best_bbox is None:
                continue
            # Validate with multi-metric matcher
            x0, y0, x1, y1 = best_bbox
            crop = band_gray[int(y0):int(y1), int(x0):int(x1)]
            disallow = None
            if page_rgb is not None:
                crop_rgb = page_rgb[int(band_y0 + y0):int(band_y0 + y1), int(x0):int(x1)]
                if crop_rgb.size > 0 and not self._is_color_candidate(crop_rgb):
                    disallow = {"veg", "non_veg"}
            label_check, score_check, margin = self._match_icon_candidate(
                crop, templates, disallow_labels=disallow
            )
            if label_check != label:
                continue
            threshold = (
                self.config.icon_score_threshold_veg
                if label_check in {"veg", "non_veg"}
                else self.config.icon_score_threshold
            )
            if score_check < threshold:
                continue
            detections.append(
                {
                    "label": label,
                    "bbox": [x0, float(band_y0 + y0), x1, float(band_y0 + y1)],
                    "score": score_check,
                }
            )
        return detections

    def _detect_icons_global(
        self,
        page_gray: np.ndarray,
        page_rgb: np.ndarray | None,
        templates: Dict[str, Dict[str, np.ndarray]],
        lines: List[Dict],
        footer_top: int,
        morph_lines: List[List[float]] | None = None,
        line_morph_map: List[int | None] | None = None,
    ) -> List[Dict]:
        if not templates or page_gray.size == 0:
            return []
        median_w, median_h = self._template_median_size(templates)
        if median_w is None or median_h is None:
            return []
        candidates = self._extract_icon_candidates_global(
            page_gray, median_w, median_h, footer_top
        )
        # Optional template scan across page to boost recall (expensive)
        if self.config.icon_full_scan:
            scan_dets = self._scan_page_for_icons(page_gray, templates, lines, footer_top)
            if scan_dets:
                for det in scan_dets:
                    candidates.append(det["bbox"])
        if not candidates:
            return []

        detections: List[Dict] = []
        for cand in candidates:
            x0, y0, x1, y1 = cand
            crop = page_gray[int(y0):int(y1), int(x0):int(x1)]
            if crop.size == 0:
                continue
            disallow = None
            if page_rgb is not None:
                crop_rgb = page_rgb[int(y0):int(y1), int(x0):int(x1)]
                if crop_rgb.size > 0 and not self._is_color_candidate(crop_rgb):
                    disallow = {"veg", "non_veg"}
            label, score, margin = self._match_icon_candidate(
                crop,
                templates,
                min_margin=self.config.icon_match_margin_global,
                disallow_labels=disallow,
            )
            if label is None or score < self.config.icon_score_threshold_global:
                continue
            detections.append({"label": label, "bbox": [x0, y0, x1, y1], "score": score})

        if not detections:
            return []

        detections = self._nms(detections, self.config.icon_iou_threshold)
        assigned = self._assign_detections_to_lines(detections, lines, footer_top, morph_lines, line_morph_map)
        return assigned

    def _extract_icon_candidates_global(
        self,
        page_gray: np.ndarray,
        median_w: int,
        median_h: int,
        footer_top: int,
    ) -> List[List[float]]:
        h, w = page_gray.shape[:2]
        work_h = max(1, min(h, footer_top))
        region = page_gray[:work_h, :]
        try:
            bw = cv2.adaptiveThreshold(
                region, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 11
            )
        except Exception:
            return []
        kernel = np.ones((3, 3), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel, iterations=2)
        bw = cv2.dilate(bw, kernel, iterations=1)
        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        if num_labels <= 1:
            return []

        candidates: List[List[float]] = []
        min_w = max(6, int(median_w * self.config.icon_global_size_min_ratio))
        max_w = int(median_w * self.config.icon_global_size_max_ratio)
        min_h = max(6, int(median_h * self.config.icon_global_size_min_ratio))
        max_h = int(median_h * self.config.icon_global_size_max_ratio)
        median_area = max(1.0, float(median_w * median_h))
        min_area = median_area * self.config.icon_global_area_min_ratio
        max_area = median_area * self.config.icon_global_area_max_ratio

        for i in range(1, num_labels):
            x, y, w_c, h_c, area = stats[i]
            if w_c < min_w or w_c > max_w:
                continue
            if h_c < min_h or h_c > max_h:
                continue
            if area < min_area or area > max_area:
                continue
            aspect = float(w_c) / float(h_c)
            if aspect < self.config.icon_candidate_aspect_min:
                continue
            if aspect > self.config.icon_candidate_aspect_max:
                continue
            candidates.append([float(x), float(y), float(x + w_c), float(y + h_c)])

        # Edge-based candidates (helps when binarization splits thin icons)
        edge = cv2.Canny(region, 50, 150)
        contours, _ = cv2.findContours(edge, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            x, y, w_c, h_c = cv2.boundingRect(cnt)
            if w_c < min_w or w_c > max_w:
                continue
            if h_c < min_h or h_c > max_h:
                continue
            area = float(cv2.contourArea(cnt))
            if area < min_area or area > max_area * 3.0:
                continue
            aspect = float(w_c) / float(h_c)
            if aspect < self.config.icon_candidate_aspect_min:
                continue
            if aspect > self.config.icon_candidate_aspect_max:
                continue
            candidates.append([float(x), float(y), float(x + w_c), float(y + h_c)])
        return candidates

    def _template_median_size(self, templates: Dict[str, Dict[str, np.ndarray]]) -> Tuple[int | None, int | None]:
        sizes = []
        for tmpl in templates.values():
            gray = tmpl.get("gray")
            if gray is None:
                continue
            h, w = gray.shape[:2]
            sizes.append((w, h))
        if not sizes:
            return None, None
        median_w = int(np.median([s[0] for s in sizes]))
        median_h = int(np.median([s[1] for s in sizes]))
        return median_w, median_h

    def _threshold_for_label(
        self,
        label: str | None,
        thresholds: Dict[str, float] | None,
        default: float | None = None,
        cap_to_base: bool = False,
    ) -> float:
        if label is None:
            return default if default is not None else self.config.icon_score_threshold
        if default is not None:
            return default
        base = (
            self.config.icon_score_threshold_veg
            if label in {"veg", "non_veg"}
            else self.config.icon_score_threshold
        )
        if thresholds and label in thresholds:
            if cap_to_base:
                return min(base, thresholds[label])
            return thresholds[label]
        return base

    def _calibrate_icon_thresholds(
        self,
        page_gray: np.ndarray,
        templates: Dict[str, Dict[str, np.ndarray]],
        footer_top: int,
    ) -> Dict[str, float]:
        if page_gray.size == 0 or not templates:
            return {}

        h, w = page_gray.shape[:2]
        footer_top = max(0, min(h, footer_top))
        footer = page_gray[footer_top:h, :]
        if footer.size == 0:
            return {}

        footer_edge = cv2.Canny(footer, 50, 150)
        pos_scores: Dict[str, float] = {}
        for label, tmpl in templates.items():
            score = self._score_template_on_region(footer, footer_edge, tmpl)
            if score is not None and score > 0:
                pos_scores[label] = score

        neg_scores: Dict[str, List[float]] = {label: [] for label in templates.keys()}
        median_w, median_h = self._template_median_size(templates)
        if median_w is None or median_h is None:
            return {}

        body_h = max(1, footer_top)
        rng = random.Random(42)
        sample_count = 24
        for _ in range(sample_count):
            scale = rng.uniform(0.8, 1.2)
            rw = int(max(6, median_w * scale))
            rh = int(max(6, median_h * scale))
            if rw >= w or rh >= body_h:
                continue
            x0 = rng.randint(0, max(1, w - rw))
            y0 = rng.randint(0, max(1, body_h - rh))
            crop = page_gray[y0 : y0 + rh, x0 : x0 + rw]
            if crop.size == 0:
                continue
            crop_edge = cv2.Canny(crop, 50, 150)
            for label, tmpl in templates.items():
                score = self._score_template_on_region(crop, crop_edge, tmpl)
                if score is not None:
                    neg_scores[label].append(score)

        thresholds: Dict[str, float] = {}
        for label in templates.keys():
            base = self.config.icon_score_threshold_veg if label in {"veg", "non_veg"} else self.config.icon_score_threshold
            pos = pos_scores.get(label)
            neg = None
            if neg_scores.get(label):
                neg = float(np.percentile(neg_scores[label], 90))
            if pos is None or pos <= 0:
                thresholds[label] = base
                continue
            target = max(base, pos * 0.85, pos - 0.08)
            if neg is not None:
                target = max(target, neg + 0.05)
            # keep below the best positive score so we don't over-prune
            target = min(target, pos - 0.02)
            if target < base:
                target = base
            thresholds[label] = float(min(0.95, max(0.2, target)))

        return thresholds

    def _score_template_on_region(
        self, region_gray: np.ndarray, region_edge: np.ndarray, tmpl: Dict[str, np.ndarray]
    ) -> float | None:
        tmpl_gray = tmpl.get("gray")
        tmpl_edge = tmpl.get("edge")
        if tmpl_gray is None or tmpl_edge is None:
            return None
        th, tw = tmpl_edge.shape[:2]
        if region_gray.size == 0:
            return None
        best_score = None
        for scale in self.config.icon_scales:
            rw = int(tw * scale)
            rh = int(th * scale)
            if rw < 6 or rh < 6:
                continue
            if rw >= region_edge.shape[1] or rh >= region_edge.shape[0]:
                continue
            resized_edge = cv2.resize(tmpl_edge, (rw, rh), interpolation=cv2.INTER_AREA)
            resized_gray = cv2.resize(tmpl_gray, (rw, rh), interpolation=cv2.INTER_AREA)
            try:
                res_edge = cv2.matchTemplate(region_edge, resized_edge, cv2.TM_CCOEFF_NORMED)
                res_gray = cv2.matchTemplate(region_gray, resized_gray, cv2.TM_CCOEFF_NORMED)
            except Exception:
                continue
            _, max_edge, _, _ = cv2.minMaxLoc(res_edge)
            _, max_gray, _, _ = cv2.minMaxLoc(res_gray)
            score = (float(max_edge) + float(max_gray)) / 2.0
            if best_score is None or score > best_score:
                best_score = score
        return best_score

    def _assign_detections_to_lines(
        self,
        detections: List[Dict],
        lines: List[Dict],
        footer_top: int,
        morph_lines: List[List[float]] | None = None,
        line_morph_map: List[int | None] | None = None,
    ) -> List[Dict]:
        if not detections or not lines:
            return []
        assigned: List[Dict] = []

        price_col_x = self._infer_price_column_x(lines, footer_top)

        morph_to_lines: Dict[int, List[int]] = {}
        if morph_lines and line_morph_map:
            for idx, m_idx in enumerate(line_morph_map):
                if m_idx is None:
                    continue
                morph_to_lines.setdefault(m_idx, []).append(idx)

        for det in detections:
            x0, y0, x1, y1 = det["bbox"]
            # Allow all detections to be candidates; filtering happens at line level
            # if y0 >= footer_top:
            #    continue
            icx = (x0 + x1) / 2.0
            icy = (y0 + y1) / 2.0
            ih = max(1.0, y1 - y0)

            best_line_idx = None
            best_score = None

            # Prefer morphology-based line boxes when available
            if morph_lines:
                for m_idx, box in enumerate(morph_lines):
                    mx0, my0, mx1, my1 = box
                    if my0 >= footer_top:
                        continue
                    mcy = (my0 + my1) / 2.0
                    mh = max(1.0, my1 - my0)
                    if abs(icy - mcy) > max(mh * self.config.icon_assign_y_factor, 70.0):
                        continue
                    if icx < mx0:
                        dx = mx0 - x1
                    elif icx > mx1:
                        dx = x0 - mx1
                    else:
                        dx = 0.0
                    if dx > (mh * self.config.icon_assign_max_dx_factor):
                        continue
                    overlap_y = max(0.0, min(y1, my1) - max(y0, my0))
                    overlap_ratio = overlap_y / (min(ih, mh) + 1e-6)
                    dy = 0.0 if overlap_ratio >= 0.2 else abs(icy - mcy)
                    score = dx + (dy * 3.0)
                    if best_score is None or score < best_score:
                        best_score = score
                        # map to title OCR line in this morph row
                        if m_idx in morph_to_lines:
                            best_line_idx = self._pick_title_line(morph_to_lines[m_idx], lines)
                        else:
                            best_line_idx = None

            if best_line_idx is None:
                for idx, line in enumerate(lines):
                    lx0, ly0, lx1, ly1 = line["bbox"]
                    is_l_protected = (
                        "kcal" in (line.get("text") or "").lower() or 
                        self._extract_price_from_line(line) is not None
                    )
                    if ly0 >= footer_top and not is_l_protected:
                        continue
                    lh = max(1.0, ly1 - ly0)
                    lcy = (ly0 + ly1) / 2.0
                    if abs(icy - lcy) > max(lh * self.config.icon_assign_y_factor, 70.0):
                        continue
                    if icx < lx0:
                        dx = lx0 - x1
                    elif icx > lx1:
                        dx = x0 - lx1
                    else:
                        dx = 0.0
                    if dx > (lh * self.config.icon_assign_max_dx_factor):
                        continue
                    overlap_y = max(0.0, min(y1, ly1) - max(y0, ly0))
                    overlap_ratio = overlap_y / (min(ih, lh) + 1e-6)
                    dy = 0.0 if overlap_ratio >= 0.2 else abs(icy - lcy)
                    score = dx + (dy * 3.0)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_line_idx = idx

            if best_line_idx is None:
                continue

            # Drop detections that overlap text too much (likely false positives)
            overlap = self._overlap_ratio(det["bbox"], lines[best_line_idx]["bbox"])
            lh = max(1.0, lines[best_line_idx]["bbox"][3] - lines[best_line_idx]["bbox"][1])
            if price_col_x is not None and icx >= (price_col_x - (lh * 0.2)):
                # Ignore detections inside the price column strip.
                continue
            if overlap > self.config.icon_text_overlap_max and not self._icon_allowed_overlap(
                det["bbox"], lines[best_line_idx]["bbox"], lh
            ):
                continue

            det = dict(det)
            det["line_index"] = best_line_idx
            assigned.append(det)

        return assigned

    def _infer_price_column_x(self, lines: List[Dict], footer_top: int) -> float | None:
        if not lines:
            return None
        price_xs: List[float] = []
        max_x = 0.0
        for line in lines:
            lx0, ly0, lx1, _ = line.get("bbox", [0.0, 0.0, 0.0, 0.0])
            if ly0 >= footer_top:
                continue
            max_x = max(max_x, float(lx1))
            text = (line.get("text") or "").strip()
            if not text:
                continue
            t = text.lower()
            for sym in ("inr", "rs", "aed", "sar", "$", "â‚¹"):
                t = t.replace(sym, "")
            t = t.replace(",", "").replace(".", "").strip()
            if t.isdigit() and len(t) <= 6:
                price_xs.append(float(lx0))
        if len(price_xs) < 4:
            return None
        price_xs.sort()
        mid = price_xs[len(price_xs) // 2]
        mad = sorted(abs(x - mid) for x in price_xs)[len(price_xs) // 2]
        if max_x <= 0.0:
            return None
        if mad > (max_x * 0.03):
            return None
        return float(mid)

    def _scan_page_for_icons(
        self,
        page_gray: np.ndarray,
        templates: Dict[str, Dict[str, np.ndarray]],
        lines: List[Dict],
        footer_top: int,
    ) -> List[Dict]:
        if page_gray.size == 0 or not templates:
            return []
        # restrict to area covered by text lines (exclude footer)
        y_min = None
        y_max = None
        for line in lines:
            ly0, ly1 = line["bbox"][1], line["bbox"][3]
            if ly0 >= footer_top:
                continue
            y_min = ly0 if y_min is None else min(y_min, ly0)
            y_max = ly1 if y_max is None else max(y_max, ly1)
        if y_min is None or y_max is None:
            return []

        y0 = max(0, int(y_min - 100))
        y1 = min(page_gray.shape[0], int(y_max + 100))
        region = page_gray[y0:y1, :]
        if region.size == 0:
            return []
        region_edge = cv2.Canny(region, 50, 150)

        detections: List[Dict] = []
        for label, tmpl in templates.items():
            tmpl_gray = tmpl.get("gray")
            tmpl_edge = tmpl.get("edge")
            if tmpl_gray is None or tmpl_edge is None:
                continue
            th, tw = tmpl_edge.shape[:2]
            candidates: List[Tuple[float, List[float]]] = []
            for scale in self.config.icon_scales:
                rw = int(tw * scale)
                rh = int(th * scale)
                if rw < 6 or rh < 6:
                    continue
                if rw >= region_edge.shape[1] or rh >= region_edge.shape[0]:
                    continue
                resized_edge = cv2.resize(tmpl_edge, (rw, rh), interpolation=cv2.INTER_AREA)
                resized_gray = cv2.resize(tmpl_gray, (rw, rh), interpolation=cv2.INTER_AREA)
                res_edge = cv2.matchTemplate(region_edge, resized_edge, cv2.TM_CCOEFF_NORMED)
                res_gray = cv2.matchTemplate(region, resized_gray, cv2.TM_CCOEFF_NORMED)
                score_map = (res_edge + res_gray) / 2.0

                # local maxima
                kernel = np.ones((3, 3), np.uint8)
                maxima = score_map == cv2.dilate(score_map, kernel)
                ys, xs = np.where((score_map >= self.config.icon_global_scan_threshold) & maxima)
                for yy, xx in zip(ys, xs):
                    edge_val = float(res_edge[yy, xx])
                    gray_val = float(res_gray[yy, xx])
                    if min(edge_val, gray_val) < self.config.icon_global_scan_gray_threshold:
                        continue
                    score = float(score_map[yy, xx])
                    bbox = [float(xx), float(y0 + yy), float(xx + rw), float(y0 + yy + rh)]
                    candidates.append((score, bbox))

            if not candidates:
                continue
            # keep top candidates per label
            candidates = sorted(candidates, key=lambda x: x[0], reverse=True)[: self.config.icon_global_max_per_label]
            for score, bbox in candidates:
                detections.append({"label": label, "bbox": bbox, "score": score})

        # verify with multi-metric matcher
        verified: List[Dict] = []
        for det in detections:
            x0, y0, x1, y1 = det["bbox"]
            crop = page_gray[int(y0):int(y1), int(x0):int(x1)]
            if crop.size == 0:
                continue
            label_check, score_check, margin = self._match_icon_candidate(
                crop, templates, min_margin=self.config.icon_match_margin_global
            )
            if label_check != det["label"]:
                continue
            if score_check < self.config.icon_score_threshold_global:
                continue
            det = dict(det)
            det["score"] = max(det["score"], score_check)
            verified.append(det)

        return verified

    def _detect_text_lines_morphology(self, page_gray: np.ndarray) -> List[List[float]]:
        if page_gray.size == 0:
            return []
        try:
            bw = cv2.adaptiveThreshold(
                page_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 11
            )
        except Exception:
            return []

        # estimate median character height from components
        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        heights = []
        for i in range(1, num_labels):
            _, _, _, h, area = stats[i]
            if area < 10:
                continue
            if h > 3:
                heights.append(h)
        median_h = int(np.median(heights)) if heights else 10

        kernel_w = max(15, median_h * 8)
        kernel_h = max(2, median_h // 2)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, kernel_h))
        merged = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: List[List[float]] = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w < (median_h * 6):
                continue
            if h < (median_h * 0.6) or h > (median_h * 4.0):
                continue
            boxes.append([float(x), float(y), float(x + w), float(y + h)])
        boxes.sort(key=lambda b: (b[1], b[0]))
        return boxes

    def _map_ocr_lines_to_morph(
        self, lines: List[Dict], morph_lines: List[List[float]]
    ) -> List[int | None]:
        if not lines or not morph_lines:
            return [None for _ in lines]
        mapping: List[int | None] = []
        for line in lines:
            lx0, ly0, lx1, ly1 = line["bbox"]
            best_idx = None
            best_score = None
            for i, box in enumerate(morph_lines):
                mx0, my0, mx1, my1 = box
                overlap_y = max(0.0, min(ly1, my1) - max(ly0, my0))
                overlap_x = max(0.0, min(lx1, mx1) - max(lx0, mx0))
                if overlap_y <= 0 or overlap_x <= 0:
                    continue
                overlap = overlap_x * overlap_y
                if best_score is None or overlap > best_score:
                    best_score = overlap
                    best_idx = i
            mapping.append(best_idx)
        return mapping

    def _pick_title_line(self, indices: List[int], lines: List[Dict]) -> int | None:
        best_idx = None
        best_key = None
        for idx in indices:
            text = lines[idx].get("text", "")
            h = abs(lines[idx]["bbox"][3] - lines[idx]["bbox"][1])
            text_len = len(text)
            dish_score = 1 if self._looks_like_dish_line(text) else 0
            key = (dish_score, h, text_len)
            if best_key is None or key > best_key:
                best_key = key
                best_idx = idx
        return best_idx

    def _overlap_ratio(self, a: List[float], b: List[float]) -> float:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        inter_x0 = max(ax0, bx0)
        inter_y0 = max(ay0, by0)
        inter_x1 = min(ax1, bx1)
        inter_y1 = min(ay1, by1)
        inter_w = max(0.0, inter_x1 - inter_x0)
        inter_h = max(0.0, inter_y1 - inter_y0)
        inter = inter_w * inter_h
        area_a = max(1.0, (ax1 - ax0) * (ay1 - ay0))
        return inter / area_a

    def _icon_allowed_overlap(self, icon_bbox: List[float], line_bbox: List[float], line_h: float) -> bool:
        ix0, _, ix1, _ = icon_bbox
        lx0, _, lx1, _ = line_bbox
        icx = (ix0 + ix1) / 2.0
        lw = max(1.0, lx1 - lx0)
        edge_pad = max(2.0, line_h * self.config.icon_edge_allow_factor)
        left_inside = lx0 + max(edge_pad, line_h * self.config.line_icon_inside_factor)
        right_half = lx0 + (lw * self.config.line_icon_right_start_ratio)
        return icx <= left_inside or icx >= (lx1 - edge_pad) or icx >= right_half

    def _is_generic_menu_header_text(self, text: str) -> bool:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if not raw:
            return False
        lower = raw.lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", lower) if t]
        if not tokens:
            return False
        if "menu" in tokens and len(tokens) <= 8:
            return True
        return False

    def _looks_like_dish_line(self, text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        if self._is_generic_menu_header_text(t):
            return False
        if re.search(r"\b\d{1,2}:\d{2}\b", t) and ("am" in t or "pm" in t):
            return False
        if any(k in t for k in ("kcal", "kcai", "kcai", "cal")):
            return True
        if any(sym in t for sym in ("Ã¢â€šÂ¹", "inr", "rs", "aed", "sar", "$")):
            return True
        # price-like numbers
        if any(ch.isdigit() for ch in t) and len(t) > 6:
            return True
        # mostly uppercase -> likely dish name header
        letters = [c for c in text if c.isalpha()]
        if letters:
            upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if upper_ratio > 0.6:
                if len(text) > 8:
                    return True
                if len(text) >= 5 and text.isupper():
                    return True
        return False

    def _extract_icon_candidates(
        self,
        page_gray: np.ndarray,
        band_y0: int,
        band_y1: int,
        roi_list: List[Tuple[int, int, bool]],
        line_h: float,
        line_bbox: List[float],
    ) -> List[List[float]]:
        candidates: List[List[float]] = []
        if band_y1 <= band_y0:
            return candidates

        band = page_gray[band_y0:band_y1, :]
        for rx0, rx1, allow_overlap in roi_list:
            if rx1 <= rx0:
                continue
            region = band[:, rx0:rx1]
            if region.size == 0:
                continue
            try:
                bw = cv2.adaptiveThreshold(
                    region, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 11
                )
            except Exception:
                continue
            kernel = np.ones((3, 3), np.uint8)
            bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel, iterations=2)
            num_labels, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
            if num_labels <= 1:
                continue
            base_area = max(1.0, line_h * line_h)
            merge_candidates: List[List[float]] = []
            for i in range(1, num_labels):
                x, y, w_c, h_c, area = stats[i]
                if w_c == 0 or h_c == 0:
                    continue
                area_ratio = float(area) / base_area
                if area_ratio >= (self.config.icon_candidate_min_area_ratio * self.config.icon_component_min_area_ratio_factor):
                    if h_c >= (line_h * self.config.icon_component_min_size_factor):
                        aspect = float(w_c) / float(h_c)
                        if (
                            aspect >= self.config.icon_candidate_aspect_min
                            and aspect <= self.config.icon_candidate_aspect_max
                        ):
                            pad = int(max(1.0, line_h * self.config.icon_candidate_pad_ratio))
                            x0 = max(0, rx0 + x - pad)
                            y0 = max(0, band_y0 + y - pad)
                            x1 = min(page_gray.shape[1], rx0 + x + w_c + pad)
                            y1 = min(page_gray.shape[0], band_y0 + y + h_c + pad)
                            merge_candidates.append([float(x0), float(y0), float(x1), float(y1)])
                if area_ratio < self.config.icon_candidate_min_area_ratio:
                    continue
                if area_ratio > self.config.icon_candidate_max_area_ratio:
                    continue
                aspect = float(w_c) / float(h_c)
                if aspect < self.config.icon_candidate_aspect_min:
                    continue
                if aspect > self.config.icon_candidate_aspect_max:
                    continue
                if h_c < (line_h * self.config.line_icon_min_size_factor):
                    continue
                if h_c > (line_h * self.config.line_icon_max_size_factor):
                    continue
                pad = int(max(1.0, line_h * self.config.icon_candidate_pad_ratio))
                x0 = max(0, rx0 + x - pad)
                y0 = max(0, band_y0 + y - pad)
                x1 = min(page_gray.shape[1], rx0 + x + w_c + pad)
                y1 = min(page_gray.shape[0], band_y0 + y + h_c + pad)
                cand = [float(x0), float(y0), float(x1), float(y1)]
                if not allow_overlap:
                    if self._overlap_ratio(cand, line_bbox) > self.config.icon_text_overlap_max:
                        continue
                else:
                    if not self._icon_allowed_overlap(cand, line_bbox, line_h):
                        continue
                candidates.append(cand)
            if merge_candidates:
                gap = float(line_h * self.config.icon_component_gap_factor)
                merged = self._merge_component_boxes(merge_candidates, gap, gap)
                for cand in merged:
                    x0, y0, x1, y1 = cand
                    w_c = max(1.0, x1 - x0)
                    h_c = max(1.0, y1 - y0)
                    area_ratio = (w_c * h_c) / base_area
                    if area_ratio < self.config.icon_candidate_min_area_ratio:
                        continue
                    if area_ratio > self.config.icon_candidate_max_area_ratio:
                        continue
                    aspect = float(w_c) / float(h_c)
                    if aspect < self.config.icon_candidate_aspect_min:
                        continue
                    if aspect > self.config.icon_candidate_aspect_max:
                        continue
                    if h_c < (line_h * self.config.line_icon_min_size_factor):
                        continue
                    if h_c > (line_h * self.config.line_icon_max_size_factor):
                        continue
                    if not allow_overlap:
                        if self._overlap_ratio(cand, line_bbox) > self.config.icon_text_overlap_max:
                            continue
                    else:
                        if not self._icon_allowed_overlap(cand, line_bbox, line_h):
                            continue
                    candidates.append(cand)
            # Edge-based candidates for low-contrast icons (helps faint gray outlines)
            try:
                edge = cv2.Canny(region, 50, 150)
                contours, _ = cv2.findContours(edge, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            except Exception:
                contours = []
            if contours:
                for cnt in contours:
                    x, y, w_c, h_c = cv2.boundingRect(cnt)
                    if w_c == 0 or h_c == 0:
                        continue
                    area = float(cv2.contourArea(cnt))
                    base_area = max(1.0, line_h * line_h)
                    area_ratio = float(area) / base_area
                    if area_ratio < self.config.icon_candidate_min_area_ratio:
                        continue
                    if area_ratio > self.config.icon_candidate_max_area_ratio * 3.0:
                        continue
                    aspect = float(w_c) / float(h_c)
                    if aspect < self.config.icon_candidate_aspect_min:
                        continue
                    if aspect > self.config.icon_candidate_aspect_max:
                        continue
                    if h_c < (line_h * self.config.line_icon_min_size_factor):
                        continue
                    if h_c > (line_h * self.config.line_icon_max_size_factor):
                        continue
                    pad = int(max(1.0, line_h * self.config.icon_candidate_pad_ratio))
                    x0 = max(0, rx0 + x - pad)
                    y0 = max(0, band_y0 + y - pad)
                    x1 = min(page_gray.shape[1], rx0 + x + w_c + pad)
                    y1 = min(page_gray.shape[0], band_y0 + y + h_c + pad)
                    cand = [float(x0), float(y0), float(x1), float(y1)]
                    if not allow_overlap:
                        if self._overlap_ratio(cand, line_bbox) > self.config.icon_text_overlap_max:
                            continue
                    else:
                        if not self._icon_allowed_overlap(cand, line_bbox, line_h):
                            continue
                    candidates.append(cand)
        return candidates

    def _merge_component_boxes(
        self,
        boxes: List[List[float]],
        gap_x: float,
        gap_y: float,
    ) -> List[List[float]]:
        if len(boxes) < 2:
            return []
        parent = list(range(len(boxes)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri = find(i)
            rj = find(j)
            if ri != rj:
                parent[rj] = ri

        def close(a: List[float], b: List[float]) -> bool:
            ax0, ay0, ax1, ay1 = a
            bx0, by0, bx1, by1 = b
            gapx = max(bx0 - ax1, ax0 - bx1, 0.0)
            gapy = max(by0 - ay1, ay0 - by1, 0.0)
            return gapx <= gap_x and gapy <= gap_y

        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if close(boxes[i], boxes[j]):
                    union(i, j)

        groups: Dict[int, List[List[float]]] = {}
        for i, box in enumerate(boxes):
            groups.setdefault(find(i), []).append(box)

        merged: List[List[float]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            x0 = min(b[0] for b in group)
            y0 = min(b[1] for b in group)
            x1 = max(b[2] for b in group)
            y1 = max(b[3] for b in group)
            merged.append([x0, y0, x1, y1])
        return merged

    def _match_icon_candidate(
        self,
        crop_gray: np.ndarray,
        templates: Dict[str, Dict[str, np.ndarray]],
        min_margin: float | None = None,
        disallow_labels: set[str] | None = None,
    ) -> Tuple[str | None, float, float]:
        if crop_gray.size == 0:
            return None, 0.0, 0.0
        if min_margin is None:
            min_margin = self.config.icon_match_margin

        try:
            _, bw_cand = cv2.threshold(crop_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        except Exception:
            return None, 0.0, 0.0

        cand_gray = crop_gray
        cand_bw = bw_cand
        # Tight crop to actual ink to stabilize IoU/SSIM scores.
        mask = self._prepare_binary_mask(cand_bw)
        if mask is not None:
            ys, xs = np.where(mask > 0)
            if ys.size > 0 and xs.size > 0:
                x0, x1 = xs.min(), xs.max()
                y0, y1 = ys.min(), ys.max()
                cand_gray = cand_gray[y0 : y1 + 1, x0 : x1 + 1]
                cand_bw = mask[y0 : y1 + 1, x0 : x1 + 1]

        cand_ratio = float(np.count_nonzero(cand_bw)) / float(cand_bw.size or 1)
        if cand_ratio < self.config.icon_fg_ratio_min or cand_ratio > self.config.icon_fg_ratio_max:
            return None, 0.0, 0.0

        # Reject text-like regions (too many connected components)
        try:
            num_labels, _ = cv2.connectedComponents(cand_bw)
            if num_labels > self.config.icon_max_components:
                return None, 0.0, 0.0
        except Exception:
            pass

        best_label = None
        best_score = -1.0
        second_score = -1.0
        for label, tmpl in templates.items():
            if disallow_labels and label in disallow_labels:
                continue
            tmpl_gray = tmpl.get("gray_trim")
            if tmpl_gray is None:
                tmpl_gray = tmpl.get("gray")
            if tmpl_gray is None:
                continue
            resized = cv2.resize(cand_gray, (tmpl_gray.shape[1], tmpl_gray.shape[0]), interpolation=cv2.INTER_AREA)
            ssim = self._ssim(resized, tmpl_gray)
            ssim_score = max(0.0, min(1.0, (ssim + 1.0) / 2.0))

            orb_score = self._orb_similarity(resized, tmpl.get("kp"), tmpl.get("des"))
            shape_score = self._shape_similarity(resized, tmpl.get("contour"))
            try:
                tmpl_score = float(cv2.matchTemplate(resized, tmpl_gray, cv2.TM_CCOEFF_NORMED)[0][0])
            except Exception:
                tmpl_score = 0.0
            tmpl_bw = tmpl.get("bw_trim")
            if tmpl_bw is None:
                tmpl_bw = tmpl.get("bw")
            if tmpl_bw is None:
                continue
            resized_bw = cv2.resize(cand_bw, (tmpl_bw.shape[1], tmpl_bw.shape[0]), interpolation=cv2.INTER_AREA)
            binary_iou = self._binary_iou_masks(resized_bw, tmpl_bw)

            if max(ssim_score, tmpl_score) < self.config.icon_min_similarity:
                continue
            tmpl_ratio = tmpl.get("fg_ratio_trim")
            if tmpl_ratio is None:
                tmpl_ratio = tmpl.get("fg_ratio")
            if tmpl_ratio is not None and tmpl_ratio > 0:
                low = tmpl_ratio / self.config.icon_fg_ratio_tol
                high = tmpl_ratio * self.config.icon_fg_ratio_tol
                if cand_ratio < low or cand_ratio > high:
                    continue
            if binary_iou < self.config.icon_binary_iou_min:
                continue

            score = (
                (ssim_score * self.config.icon_ssim_weight)
                + (orb_score * self.config.icon_orb_weight)
                + (shape_score * self.config.icon_shape_weight)
                + (tmpl_score * self.config.icon_template_weight)
                + (binary_iou * self.config.icon_binary_iou_weight)
            )
            if score > best_score:
                second_score = best_score
                best_score = score
                best_label = label
            elif score > second_score:
                second_score = score

        margin = best_score - second_score if second_score >= 0 else best_score
        if margin < min_margin:
            return None, float(best_score), float(margin)

        return best_label, float(best_score), float(margin)

    def _ssim(self, a: np.ndarray, b: np.ndarray) -> float:
        if a.shape != b.shape:
            return 0.0
        a = a.astype(np.float32)
        b = b.astype(np.float32)
        mu_a = a.mean()
        mu_b = b.mean()
        sigma_a = ((a - mu_a) ** 2).mean()
        sigma_b = ((b - mu_b) ** 2).mean()
        sigma_ab = ((a - mu_a) * (b - mu_b)).mean()
        c1 = 6.5025
        c2 = 58.5225
        denom = (mu_a * mu_a + mu_b * mu_b + c1) * (sigma_a + sigma_b + c2)
        if denom == 0:
            return 0.0
        return ((2 * mu_a * mu_b + c1) * (2 * sigma_ab + c2)) / denom

    def _orb_similarity(self, cand_gray: np.ndarray, tmpl_kp, tmpl_des) -> float:
        if tmpl_des is None:
            return 0.0
        kp, des = self._orb.detectAndCompute(cand_gray, None)
        if des is None or kp is None or tmpl_kp is None:
            return 0.0
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        try:
            matches = bf.match(des, tmpl_des)
        except Exception:
            return 0.0
        if not matches:
            return 0.0
        matches = sorted(matches, key=lambda m: m.distance)
        # Normalize by number of keypoints to keep 0..1
        good = sum(1 for m in matches if m.distance < 60)
        denom = max(len(kp), len(tmpl_kp), 1)
        return min(1.0, float(good) / float(denom))

    def _shape_similarity(self, cand_gray: np.ndarray, tmpl_contour) -> float:
        if tmpl_contour is None:
            return 0.0
        edge = cv2.Canny(cand_gray, 50, 150)
        contour = self._largest_contour(edge)
        if contour is None:
            return 0.0
        try:
            dist = cv2.matchShapes(contour, tmpl_contour, cv2.CONTOURS_MATCH_I1, 0.0)
        except Exception:
            return 0.0
        return 1.0 / (1.0 + float(dist))

    def _classify_veg_nonveg(self, crop_rgb: np.ndarray, relaxed: bool = False) -> Tuple[str | None, float]:
        if crop_rgb is None or crop_rgb.size == 0:
            return None, 0.0
        try:
            gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return None, 0.0
        h, w = gray.shape[:2]
        if h < 6 or w < 6:
            return None, 0.0
        # Require strong color saturation (veg/non-veg icons are colored squares)
        try:
            hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
            hue = hsv[:, :, 0]
            sat = hsv[:, :, 1]
            val = hsv[:, :, 2]
            sat_thresh = 60 if not relaxed else 45
            val_thresh = 40 if not relaxed else 30
            color_mask = (sat >= sat_thresh) & (val >= val_thresh)
            if int(color_mask.sum()) < max(20, int(0.005 * float(h * w))):
                return None, 0.0
        except Exception:
            return None, 0.0
        try:
            _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        except Exception:
            return None, 0.0
        contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None, 0.0
        cnt = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(cnt)
        if area < 12:
            return None, 0.0
        x, y, cw, ch = cv2.boundingRect(cnt)
        aspect = float(cw) / float(ch) if ch else 0.0
        if aspect < 0.85 or aspect > 1.18:
            return None, 0.0
        rect_area = float(cw * ch) if (cw * ch) else 0.0
        rect_ratio = (area / rect_area) if rect_area else 0.0
        if rect_ratio < 0.9:
            return None, 0.0
        inner = bw[y : y + ch, x : x + cw]
        kernel = np.ones((3, 3), np.uint8)
        inner = cv2.erode(inner, kernel, iterations=1)
        inner_contours, _ = cv2.findContours(inner, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not inner_contours:
            return None, 0.0
        inner_cnt = max(inner_contours, key=cv2.contourArea)
        inner_area = cv2.contourArea(inner_cnt)
        if inner_area < 6:
            return None, 0.0
        # Decide label by color dominance (red vs green)
        try:
            hue_vals = hue[color_mask]
            if hue_vals.size == 0:
                return None, 0.0
            red = int(((hue_vals <= 10) | (hue_vals >= 170)).sum())
            green = int(((hue_vals >= 35) & (hue_vals <= 85)).sum())
            if red > int(green * 1.1):
                return "non_veg", 0.95 if not relaxed else 0.9
            if green > int(red * 1.1):
                return "veg", 0.95 if not relaxed else 0.9
        except Exception:
            return None, 0.0
        return None, 0.0

    def _is_color_candidate(self, crop_rgb: np.ndarray) -> bool:
        if crop_rgb is None or crop_rgb.size == 0:
            return False
        try:
            hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
            sat = hsv[:, :, 1]
            return float(sat.mean()) >= self.config.icon_color_sat_min
        except Exception:
            return False

    def _binary_iou(self, cand_gray: np.ndarray, tmpl_bw: np.ndarray | None) -> float:
        if tmpl_bw is None:
            return 0.0
        try:
            _, cand_bw = cv2.threshold(cand_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        except Exception:
            return 0.0
        cand = cand_bw > 0
        tmpl = tmpl_bw > 0
        inter = np.logical_and(cand, tmpl).sum()
        union = np.logical_or(cand, tmpl).sum()
        if union == 0:
            return 0.0
        return float(inter) / float(union)

    def _binary_iou_masks(self, cand_bw: np.ndarray, tmpl_bw: np.ndarray | None) -> float:
        if tmpl_bw is None:
            return 0.0
        cand = cand_bw > 0
        tmpl = tmpl_bw > 0
        inter = np.logical_and(cand, tmpl).sum()
        union = np.logical_or(cand, tmpl).sum()
        if union == 0:
            return 0.0
        return float(inter) / float(union)

    def _prepare_binary_mask(self, bw: np.ndarray) -> np.ndarray | None:
        if bw is None or bw.size == 0:
            return None
        mask = (bw > 0).astype(np.uint8) * 255
        # keep all meaningful components (not just the largest) so multi-part icons survive
        try:
            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            if num_labels <= 1:
                return mask
            areas = stats[1:, cv2.CC_STAT_AREA]
            if areas.size == 0:
                return mask
            largest = float(areas.max())
            min_keep = max(8, int(largest * 0.01))
            keep = np.zeros(mask.shape, dtype=np.uint8)
            for i, area in enumerate(areas, start=1):
                if area >= min_keep:
                    keep[labels == i] = 255
            if np.count_nonzero(keep) == 0:
                return mask
            return keep
        except Exception:
            return mask

    def _tight_crop_mask(self, mask: np.ndarray) -> np.ndarray | None:
        ys, xs = np.where(mask > 0)
        if ys.size == 0 or xs.size == 0:
            return None
        y0, y1 = ys.min(), ys.max()
        x0, x1 = xs.min(), xs.max()
        return mask[y0 : y1 + 1, x0 : x1 + 1]

    def _shape_similarity_bw(self, cand_bw: np.ndarray, tmpl_contour) -> float:
        if tmpl_contour is None or cand_bw is None or cand_bw.size == 0:
            return 0.0
        try:
            contours, _ = cv2.findContours(cand_bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        except Exception:
            return 0.0
        if not contours:
            return 0.0
        contour = max(contours, key=cv2.contourArea)
        try:
            dist = cv2.matchShapes(contour, tmpl_contour, cv2.CONTOURS_MATCH_I1, 0.0)
        except Exception:
            return 0.0
        return 1.0 / (1.0 + float(dist))

    def _largest_contour(self, edge: np.ndarray):
        contours, _ = cv2.findContours(edge, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        return max(contours, key=cv2.contourArea)

    def _attach_icons_to_lines(self, lines: List[Dict], icons: List[Dict]) -> List[Dict]:
        if not lines or not icons:
            return lines

        for icon in icons:
            ix0, iy0, ix1, iy1 = icon["bbox"]
            icx = (ix0 + ix1) / 2.0
            icy = (iy0 + iy1) / 2.0
            ih = max(1.0, iy1 - iy0)

            best_idx = None
            best_score = None
            for idx, line in enumerate(lines):
                lx0, ly0, lx1, ly1 = line["bbox"]
                lh = max(1.0, ly1 - ly0)
                lcy = (ly0 + ly1) / 2.0

                overlap_y = max(0.0, min(iy1, ly1) - max(iy0, ly0))
                overlap_ratio = overlap_y / (min(ih, lh) + 1e-6)
                if overlap_ratio < 0.2 and abs(icy - lcy) > (lh * self.config.line_icon_y_factor):
                    continue

                # icon should be left of the line or slightly overlapping the left edge
                if icx > (lx0 + lh * 1.5):
                    continue

                if ix1 <= lx0:
                    dx = lx0 - ix1
                elif ix0 >= lx1:
                    dx = ix0 - lx1
                else:
                    dx = 0.0

                if dx > (lh * self.config.line_icon_max_dx_factor):
                    continue

                dy = 0.0 if overlap_ratio >= 0.2 else abs(icy - lcy)
                score = dx + (dy * 0.2)
                if best_score is None or score < best_score:
                    best_score = score
                    best_idx = idx

            if best_idx is not None:
                lines[best_idx]["icons"].append(icon["label"])

        for line in lines:
            if line["icons"]:
                line["icons"] = sorted(set(line["icons"]))

        return lines

    def _estimate_column_mid(self, lines: List[Dict], page_w: float) -> float | None:
        if not lines or page_w <= 0:
            return None
        if len(lines) < 10:
            return None
        col_candidates: List[Dict] = []
        for line in lines:
            b = line.get("bbox", [0, 0, 0, 0])
            width = max(0.0, b[2] - b[0])
            if width < (page_w * 0.62):
                col_candidates.append(line)
        if len(col_candidates) < 10:
            return None
        centers = []
        for line in col_candidates:
            b = line.get("bbox", [0, 0, 0, 0])
            centers.append((b[0] + b[2]) / 2.0)
        xs = centers
        c1 = float(np.percentile(xs, 30))
        c2 = float(np.percentile(xs, 70))
        if c1 > c2:
            c1, c2 = c2, c1
        for _ in range(8):
            g1 = [x for x in xs if abs(x - c1) <= abs(x - c2)]
            g2 = [x for x in xs if abs(x - c2) < abs(x - c1)]
            if g1:
                c1 = sum(g1) / len(g1)
            if g2:
                c2 = sum(g2) / len(g2)
        if c1 > c2:
            c1, c2 = c2, c1
        gap = c2 - c1
        if gap < (page_w * 0.22):
            return None
        mid = (c1 + c2) / 2.0
        left_lines = [line for line in col_candidates if ((line.get("bbox", [0, 0, 0, 0])[0] + line.get("bbox", [0, 0, 0, 0])[2]) / 2.0) <= mid]
        right_lines = [line for line in col_candidates if ((line.get("bbox", [0, 0, 0, 0])[0] + line.get("bbox", [0, 0, 0, 0])[2]) / 2.0) > mid]
        if not left_lines or not right_lines:
            return None
        balance = min(len(left_lines), len(right_lines)) / max(len(left_lines), len(right_lines))
        if balance < 0.18:
            return None
        left_x1s = [l.get("bbox", [0, 0, 0, 0])[2] for l in left_lines]
        right_x0s = [l.get("bbox", [0, 0, 0, 0])[0] for l in right_lines]
        right_x1s = [l.get("bbox", [0, 0, 0, 0])[2] for l in right_lines]
        if not left_x1s or not right_x0s or not right_x1s:
            return None
        left_x1 = float(np.percentile(left_x1s, 85))
        right_x0 = float(np.percentile(right_x0s, 15))
        gap = right_x0 - left_x1
        if gap < (page_w * 0.04):
            return None
        right_x1 = float(np.percentile(right_x1s, 85))
        right_w = right_x1 - right_x0
        if right_w < (page_w * 0.38):
            price_ratio = self._price_like_ratio(right_lines)
            if right_w < (page_w * 0.25) and price_ratio >= 0.3:
                return None
            if price_ratio >= 0.45:
                return None
        return mid

    def _propagate_icons_to_rows(self, lines: List[Dict]) -> List[Dict]:
        if not lines:
            return lines
        line_cols = None
        explicit_cols = []
        for line in lines:
            c = line.get("column_index")
            if isinstance(c, (int, float)):
                ci = int(c)
                if ci > 0:
                    explicit_cols.append(ci)
        if explicit_cols:
            line_cols = []
            for line in lines:
                c = line.get("column_index")
                if isinstance(c, (int, float)):
                    line_cols.append(int(c))
                else:
                    line_cols.append(0)
        else:
            page_w = max((l.get("bbox", [0, 0, 0, 0])[2] for l in lines), default=0.0)
            col_mid = self._estimate_column_mid(lines, page_w)
            if col_mid is not None:
                line_cols = []
                for line in lines:
                    bx0, _, bx1, _ = line.get("bbox", [0, 0, 0, 0])
                    cx = (bx0 + bx1) / 2.0
                    line_cols.append(1 if cx <= col_mid else 2)
        heights = [abs(l["bbox"][3] - l["bbox"][1]) for l in lines if l.get("bbox")]
        if not heights:
            return lines
        median_h = float(np.median(heights))
        tol = max(2.0, median_h * self.config.row_merge_tol)
        max_gap = max(4.0, median_h * self.config.row_merge_x_gap_factor)

        sorted_idx = sorted(range(len(lines)), key=lambda i: (lines[i]["bbox"][1], lines[i]["bbox"][0]))
        rows: List[List[int]] = []

        def x_overlap_ratio(a: List[float], b: List[float]) -> float:
            ax0, _, ax1, _ = a
            bx0, _, bx1, _ = b
            inter = max(0.0, min(ax1, bx1) - max(ax0, bx0))
            denom = max(1.0, min(ax1 - ax0, bx1 - bx0))
            return inter / denom

        def x_gap(a: List[float], b: List[float]) -> float:
            ax0, _, ax1, _ = a
            bx0, _, bx1, _ = b
            if ax1 < bx0:
                return bx0 - ax1
            if bx1 < ax0:
                return ax0 - bx1
            return 0.0

        for idx in sorted_idx:
            y0, y1 = lines[idx]["bbox"][1], lines[idx]["bbox"][3]
            cy = (y0 + y1) / 2.0
            if not rows:
                rows.append([idx])
                continue
            last_row = rows[-1]
            last_idx = last_row[-1]
            ly0, ly1 = lines[last_idx]["bbox"][1], lines[last_idx]["bbox"][3]
            lcy = (ly0 + ly1) / 2.0
            overlap = x_overlap_ratio(lines[idx]["bbox"], lines[last_idx]["bbox"])
            same_col = True
            if line_cols is not None:
                cur_col = line_cols[idx]
                prev_col = line_cols[last_idx]
                same_col = (cur_col == prev_col) or (cur_col <= 0) or (prev_col <= 0)
            noise_merge = False
            if same_col:
                gap = x_gap(lines[idx]["bbox"], lines[last_idx]["bbox"])
                if gap <= max_gap:
                    if (lines[idx].get("icons") or lines[last_idx].get("icons")) and (
                        self._looks_like_icon_noise_line(lines[idx].get("text", ""))
                        or self._looks_like_icon_noise_line(lines[last_idx].get("text", ""))
                    ):
                        noise_merge = True
            if abs(cy - lcy) <= tol and (overlap >= self.config.row_merge_x_overlap or noise_merge):
                last_row.append(idx)
            else:
                rows.append([idx])

        for row in rows:
            row_icons: List[str] = []
            row_scores: Dict[str, float] = {}
            for idx in row:
                row_icons.extend(lines[idx].get("icons", []))
                scores = lines[idx].get("_icon_scores", {})
                if scores:
                    for label, score in scores.items():
                        prev = row_scores.get(label, -1.0)
                        if score > prev:
                            row_scores[label] = score
            if not row_icons:
                continue
            uniq = sorted(set(row_icons))
            # Resolve veg/non_veg conflicts using scores if available
            if "veg" in uniq and "non_veg" in uniq:
                s_veg = row_scores.get("veg", 0.0)
                s_non = row_scores.get("non_veg", 0.0)
                if s_veg >= s_non:
                    uniq = [u for u in uniq if u != "non_veg"]
                else:
                    uniq = [u for u in uniq if u != "veg"]
            # Attach icons to the title line (largest height, then longest text)
            best_idx = self._pick_title_line(row, lines)
            for idx in row:
                lines[idx]["icons"] = []
            if best_idx is not None:
                lines[best_idx]["icons"] = uniq

        # clean internal scores
        for line in lines:
            if "_icon_scores" in line:
                line.pop("_icon_scores", None)

        return lines

    def _filter_icons_to_dish_lines(self, lines: List[Dict]) -> List[Dict]:
        for line in lines:
            if not line.get("icons"):
                continue
            if not self._looks_like_dish_line(line.get("text", "")):
                line["icons"] = []
        return lines

    def _nms(self, dets: List[Dict], iou_thresh: float) -> List[Dict]:
        if not dets:
            return []
        dets = sorted(dets, key=lambda d: d["score"], reverse=True)
        keep: List[Dict] = []
        while dets:
            best = dets.pop(0)
            keep.append(best)
            dets = [d for d in dets if self._iou(best["bbox"], d["bbox"]) < iou_thresh]
        return keep

    def _iou(self, a: List[float], b: List[float]) -> float:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        inter_x0 = max(ax0, bx0)
        inter_y0 = max(ay0, by0)
        inter_x1 = min(ax1, bx1)
        inter_y1 = min(ay1, by1)
        inter_w = max(0.0, inter_x1 - inter_x0)
        inter_h = max(0.0, inter_y1 - inter_y0)
        inter = inter_w * inter_h
        if inter <= 0:
            return 0.0
        area_a = (ax1 - ax0) * (ay1 - ay0)
        area_b = (bx1 - bx0) * (by1 - by0)
        return inter / (area_a + area_b - inter + 1e-6)

    def _structure_with_openai(self, page_data: Dict) -> Dict:
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        if not api_key:
            return {"error": "OPENAI_API_KEY not set", "page": page_data.get("page")}

        client = OpenAI(api_key=api_key)

        system = (
            "You are a menu parser. Given OCR lines (with icon labels already attached per line) "
            "return a clean JSON object that includes ALL text. "
            "Output JSON only with keys: page, headings, menu_items, footer_text, other_text. "
            "Do not drop any text: if a line is not a menu item, place it in headings, footer_text, or other_text. "
            "Preserve calorie/kcal lines. Use the line-level icons list for menu items."
        )

        user = {
            "page": page_data.get("page"),
            "lines": page_data.get("lines", []),
            "page_text": page_data.get("page_text", ""),
        }

        try:
            response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user)},
            ],
            timeout=self.config.openai_timeout,
            )
        except Exception as exc:
            return {"page": page_data.get("page"), "error": str(exc)}

        try:
            text = response.output[0].content[0].text
        except Exception:
            text = ""

        parsed = self._parse_json_maybe(text)
        if parsed is not None:
            if isinstance(parsed, dict) and "page" not in parsed:
                parsed["page"] = page_data.get("page")
            parsed.setdefault("all_lines", page_data.get("lines", []))
            return parsed
        return {"page": page_data.get("page"), "raw": text, "all_lines": page_data.get("lines", [])}

    def format_menu_with_openai(self, menu_raw: Dict) -> Dict:
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        seed = self._build_items_seed(menu_raw)
        deterministic = self._format_from_seed(menu_raw, seed)
        if not api_key:
            return deterministic

        client = OpenAI(api_key=api_key)

        system = (
            "You are a menu formatter. Given OCR output JSON for a full menu, "
            "return a single clean JSON object. Output JSON only. "
            "Required top-level keys: menu_name, items, other_text, footer_text, notes. "
            "Optional top-level key allowed: extra_sections (object/array) only when explicitly present in the menu text. "
            "Each item must include: name, price, kcal, description, allergens, veg, non_veg, page, dish_type, timings. "
            "Optional item key allowed: extra_attributes (object) for explicit non-standard item metadata (e.g., spicy level, chef special markers). "
            "If a dish has an explicit short marker in brackets like (VG), (V), or (S), preserve it in "
            "item.extra_attributes.dietary_marker exactly as shown (without brackets). "
            "Use the line-level icons list as allergens and veg/non_veg flags. "
            "If line objects include name_text/price_text/kcal_text, prefer those fields. "
            "If items_seed is provided, treat it as the canonical list of items: "
            "keep item count and order identical to items_seed. "
            "Only improve descriptions and formatting; do not invent or drop items. "
            "Ignore footer/legend text (e.g., 'Kindly inform...', 'All prices are...', "
            "'calorie content' lines) and do not include it in item descriptions. "
            "Allergens must come only from icons (exclude veg/non_veg from allergens). "
            "Do not hallucinate. If a field is missing, use null or empty values. "
            "If OCR columns or words are mixed or interchanged, normalize them by meaning: "
            "infer the most sensible structure (name, price, kcal, description) using context, "
            "but do not invent details that are not supported by the text. "
            "If a page/section heading indicates dish type (e.g., breakfast/lunch/dinner/soups), "
            "set dish_type for every item in that page/section. "
            "If a heading appears directly above/before one or more dishes, use that heading text as dish_type for those dishes. "
            "Do not leave dish_type empty when such a heading is present. "
            "If no heading context is present for a dish, keep dish_type as null. "
            "If a timing window exists (e.g., 6:30 AM TO 10:30 AM), set timings for all items in that page/section. "
            "If one explicit price is followed by multiple dishes without their own prices, carry that same price "
            "to subsequent dishes until a new explicit price appears. "
            "If multiple labeled prices are present for one dish (e.g., glass/bottle or small/medium/large), "
            "also include item.extra_attributes.price_options as an object mapping each label to its price. "
            "Set kcal only when explicitly marked by kcal/cal/calorie text; do not treat plain prices as kcal."
        )

        user = {"menu_raw": menu_raw, "items_seed": seed}

        try:
            response = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(user)},
                ],
                timeout=self.config.openai_timeout,
                temperature=0.2,
            )
        except Exception as exc:
            return {"error": str(exc)}

        try:
            text = response.output[0].content[0].text
        except Exception:
            text = ""

        parsed = self._parse_json_maybe(text)
        if not parsed or not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            return deterministic
        return self._merge_openai_with_seed(parsed, deterministic)

    def format_menu_with_openai_result(self, menu_raw: Dict) -> Dict:
        seed = self._build_items_seed(menu_raw)
        deterministic = self._format_from_seed(menu_raw, seed)
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        if not api_key:
            return {
                "formatted": deterministic,
                "openai_raw": None,
                "openai_parsed": None,
                "source": "deterministic",
                "error": "OPENAI_API_KEY not set",
            }

        client = OpenAI(api_key=api_key)
        system = (
            "You are a menu formatter. Given OCR output JSON for a full menu, "
            "return a single clean JSON object. Output JSON only. "
            "Required top-level keys: menu_name, items, other_text, footer_text, notes. "
            "Optional top-level key allowed: extra_sections (object/array) only when explicitly present in the menu text. "
            "Each item must include: name, price, kcal, description, allergens, veg, non_veg, page, dish_type, timings. "
            "Optional item key allowed: extra_attributes (object) for explicit non-standard item metadata (e.g., spicy level, chef special markers). "
            "If a dish has an explicit short marker in brackets like (VG), (V), or (S), preserve it in "
            "item.extra_attributes.dietary_marker exactly as shown (without brackets). "
            "Use the line-level icons list as allergens and veg/non_veg flags. "
            "If line objects include name_text/price_text/kcal_text, prefer those fields. "
            "If items_seed is provided, treat it as the canonical list of items: "
            "keep item count and order identical to items_seed. "
            "Only improve descriptions and formatting; do not invent or drop items. "
            "Ignore footer/legend text (e.g., 'Kindly inform...', 'All prices are...', "
            "'calorie content' lines) and do not include it in item descriptions. "
            "Allergens must come only from icons (exclude veg/non_veg from allergens). "
            "Do not hallucinate. If a field is missing, use null or empty values. "
            "If a page/section heading indicates dish type (e.g., breakfast/lunch/dinner/soups), "
            "set dish_type for every item in that page/section. "
            "If a heading appears directly above/before one or more dishes, use that heading text as dish_type for those dishes. "
            "Do not leave dish_type empty when such a heading is present. "
            "If no heading context is present for a dish, keep dish_type as null. "
            "If a timing window exists (e.g., 6:30 AM TO 10:30 AM), set timings for all items in that page/section. "
            "If one explicit price is followed by multiple dishes without their own prices, carry that same price "
            "to subsequent dishes until a new explicit price appears. "
            "If multiple labeled prices are present for one dish (e.g., glass/bottle or small/medium/large), "
            "also include item.extra_attributes.price_options as an object mapping each label to its price. "
            "Set kcal only when explicitly marked by kcal/cal/calorie text; do not treat plain prices as kcal."
        )
        user = {"menu_raw": menu_raw, "items_seed": seed}
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(user)},
                ],
                timeout=self.config.openai_timeout,
                temperature=0.2,
            )
        except Exception as exc:
            return {
                "formatted": deterministic,
                "openai_raw": None,
                "openai_parsed": None,
                "source": "deterministic",
                "error": str(exc),
            }

        try:
            raw_text = response.output[0].content[0].text
        except Exception:
            raw_text = ""

        parsed = self._parse_json_maybe(raw_text)
        if not parsed or not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            return {
                "formatted": deterministic,
                "openai_raw": raw_text,
                "openai_parsed": None,
                "source": "deterministic",
                "error": "OpenAI returned invalid JSON",
            }

        merged = self._merge_openai_with_seed(parsed, deterministic)
        return {
            "formatted": merged,
            "openai_raw": raw_text,
            "openai_parsed": parsed,
            "source": "openai_merge",
            "error": None,
        }

    def docai_raw_text(self, docai: Dict[str, Any]) -> str:
        doc = self._docai_get_document(docai) if isinstance(docai, dict) else {}
        text = doc.get("text", "")
        if isinstance(text, str):
            return text
        return ""

    def pdf_raw_text(self, pdf_path: Path) -> str:
        try:
            doc = fitz.open(pdf_path)
        except Exception:
            return ""
        parts: List[str] = []
        for page_index, page in enumerate(doc):
            try:
                text = page.get_text("text")
            except Exception:
                text = ""
            text = str(text or "").strip()
            if not text:
                continue
            parts.append(f"[PAGE {page_index + 1}]\n[BODY]\n{text}")
        return "\n\n".join(parts).strip()

    def extract_pdf_text_raw(self, pdf_path: Path) -> str:
        try:
            doc = fitz.open(pdf_path)
        except Exception:
            return ""
        parts: List[str] = []
        for page in doc:
            try:
                text = page.get_text("text")
            except Exception:
                text = ""
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    def _text_quality_score(self, text: str) -> float:
        t = (text or "").strip()
        if not t:
            return 0.0
        letters = sum(1 for c in t if c.isalpha())
        total = len(t)
        alpha_ratio = letters / max(total, 1)
        words = re.findall(r"[A-Za-z]{3,}", t)
        word_tokens = re.findall(r"[A-Za-z0-9]+", t)
        long_word_ratio = len(words) / max(len(word_tokens), 1)
        short_tokens = sum(1 for tok in word_tokens if len(tok) <= 2)
        short_ratio = short_tokens / max(len(word_tokens), 1)
        raw_tokens = re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9\\s]+", t)
        junk_tokens = sum(1 for tok in raw_tokens if re.fullmatch(r"[^A-Za-z0-9]+", tok))
        junk_ratio = junk_tokens / max(len(raw_tokens), 1)
        return alpha_ratio + (0.6 * long_word_ratio) - (0.4 * short_ratio) - (0.2 * junk_ratio)

    def extract_pdf_text_lines(self, pdf_path: Path) -> Dict[str, Any]:
        try:
            doc = fitz.open(pdf_path)
        except Exception:
            return {"pdf": str(pdf_path), "pages": []}
        pages: List[Dict[str, Any]] = []
        for page_index, page in enumerate(doc):
            try:
                words = page.get_text("words")
            except Exception:
                words = []
            word_items: List[Word] = []
            for w in words:
                if len(w) < 5:
                    continue
                x0, y0, x1, y1, text = w[:5]
                if not text or not str(text).strip():
                    continue
                word_items.append((float(x0), float(y0), float(x1), float(y1), str(text)))
            page_w = float(page.rect.width) if page.rect.width else 0.0
            if not word_items:
                lines = []
            else:
                columns = self._split_words_into_columns(word_items, page_w)
                if len(columns) <= 1:
                    lines = self._group_words_into_lines(word_items, page_width=page_w)
                else:
                    lines = []
                    for col_idx, col_words in enumerate(columns, start=1):
                        if not col_words:
                            continue
                        xs0 = [w[0] for w in col_words]
                        xs1 = [w[2] for w in col_words]
                        bounds = (int(min(xs0)), int(max(xs1))) if xs0 and xs1 else None
                        lines.extend(
                            self._group_words_into_lines(
                                col_words,
                                page_width=page_w,
                                column_index=col_idx,
                                column_bounds=bounds,
                            )
                        )
            pages.append({"page": page_index + 1, "lines": lines})
        return {"pdf": str(pdf_path), "pages": pages}

    def pdf_text_usable(self, text: str) -> bool:
        if not text:
            return False
        letters = sum(1 for c in text if c.isalpha())
        words = len(re.findall(r"[A-Za-z]{2,}", text))
        if words < 40:
            return False
        ratio = letters / max(len(text), 1)
        return ratio >= 0.2

    def _split_words_into_columns(self, words: List[Word], page_w: float) -> List[List[Word]]:
        if not words or page_w <= 0:
            return [words]
        if len(words) < 40:
            return [words]
        centers = [((w[0] + w[2]) / 2.0, w) for w in words]
        xs = [c[0] for c in centers]
        c1 = float(np.percentile(xs, 30))
        c2 = float(np.percentile(xs, 70))
        if c1 > c2:
            c1, c2 = c2, c1
        for _ in range(8):
            g1 = [x for x in xs if abs(x - c1) <= abs(x - c2)]
            g2 = [x for x in xs if abs(x - c2) < abs(x - c1)]
            if g1:
                c1 = sum(g1) / len(g1)
            if g2:
                c2 = sum(g2) / len(g2)
        if c1 > c2:
            c1, c2 = c2, c1
        mid = (c1 + c2) / 2.0
        left = [w for x, w in centers if x <= mid]
        right = [w for x, w in centers if x > mid]
        if not left or not right:
            return [words]
        balance = min(len(left), len(right)) / max(len(left), len(right))
        if balance < 0.25:
            return [words]
        left_x1 = max(w[2] for w in left)
        right_x0 = min(w[0] for w in right)
        gap = right_x0 - left_x1
        if gap < (page_w * 0.04):
            return [words]
        right_w = max(w[2] for w in right) - min(w[0] for w in right)
        if right_w < (page_w * 0.38):
            price_ratio = self._price_like_ratio_words(right)
            if price_ratio >= 0.55:
                return [words]
        return [left, right]

    def _price_like_ratio_words(self, words: List[Word]) -> float:
        if not words:
            return 0.0
        price_like = 0
        for _, _, _, _, text in words:
            t = str(text).lower().strip()
            if not t:
                continue
            if any(sym in t for sym in ("Ã¢â€šÂ¹", "inr", "rs", "aed", "sar", "$")):
                price_like += 1
                continue
            t = t.replace(",", "").replace(".", "")
            if t.isdigit():
                price_like += 1
        return price_like / max(len(words), 1)

    def _is_price_like_layout_line(self, text: str) -> bool:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if not raw:
            return False
        lower = raw.lower()
        # Timing windows are not price-only lines.
        if re.search(r"\b\d{1,2}:\d{2}\b", lower) and ("am" in lower or "pm" in lower):
            return False
        numeric_chunks = re.findall(r"\d+(?:[.,]\d+)?(?:/\d+(?:[.,]\d+)?)?", raw)
        if not numeric_chunks:
            return False
        alpha_count = sum(1 for ch in raw if ch.isalpha())
        digit_count = sum(1 for ch in raw if ch.isdigit())
        if digit_count <= 0:
            return False
        if alpha_count == 0:
            return True
        if alpha_count <= 4 and ("kcal" in lower or re.search(r"\bcal\b", lower)):
            return True
        currency_only = re.sub(r"(rs|inr|aed|sar|usd|eur|\$|₹|kcal|kcai|cal)", " ", lower)
        currency_only = re.sub(r"[^a-z]+", "", currency_only)
        if not currency_only:
            return True
        return False

    def _collect_explicit_columns(self, lines: List[Dict]) -> Tuple[List[Dict], List[List[Dict]], List[Dict]]:
        shared: List[Dict] = []
        footer: List[Dict] = []
        col_map: Dict[int, List[Dict]] = {}
        for line in lines:
            if not isinstance(line, dict):
                continue
            role = str(line.get("layout_role") or "").strip().lower()
            col_raw = line.get("column_index")
            col_idx = None
            if isinstance(col_raw, (int, float)):
                try:
                    col_idx = int(col_raw)
                except Exception:
                    col_idx = None
            if role == "footer":
                footer.append(line)
                continue
            if col_idx is None:
                continue
            if col_idx <= 0:
                if role == "footer":
                    footer.append(line)
                else:
                    shared.append(line)
            else:
                col_map.setdefault(col_idx, []).append(line)
        columns = [col_map[k] for k in sorted(col_map.keys())]
        return shared, columns, footer

    def _annotate_layout_columns(self, lines: List[Dict]) -> List[Dict]:
        if not lines:
            return lines

        page_w = max((float((l.get("bbox") or [0, 0, 0, 0])[2]) for l in lines if isinstance(l, dict)), default=0.0)
        page_h = max((float((l.get("bbox") or [0, 0, 0, 0])[3]) for l in lines if isinstance(l, dict)), default=0.0)
        if page_w <= 0:
            return lines

        for line in lines:
            if not isinstance(line, dict):
                continue
            line.pop("layout_role", None)
            line.pop("column_index", None)
            line.pop("column_bbox", None)

        heights = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            b = line.get("bbox", [0, 0, 0, 0])
            try:
                h = float(b[3]) - float(b[1])
            except Exception:
                h = 0.0
            if h > 0:
                heights.append(h)
        median_h = float(np.median(heights)) if heights else 24.0

        body_lines: List[Dict] = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            text = (line.get("text") or "").strip()
            if not text:
                continue
            b = line.get("bbox", [0, 0, 0, 0])
            y0 = float(b[1]) if len(b) >= 2 else 0.0
            is_footer = self._is_footer_text(text)
            if not is_footer and page_h > 0 and y0 >= (page_h * 0.65) and self._looks_like_legend_line(text):
                is_footer = True
            if is_footer:
                line["layout_role"] = "footer"
                line["column_index"] = 0
                line["column_bbox"] = [0.0, round(float(page_w), 1)]
                continue
            body_lines.append(line)

        if not body_lines:
            return lines

        shared_lines: List[Dict] = []
        anchor_lines: List[Dict] = []
        for line in body_lines:
            b = line.get("bbox", [0, 0, 0, 0])
            x0, _, x1, _ = [float(v) for v in b]
            width = max(0.0, x1 - x0)
            text = (line.get("text") or "").strip()
            is_price = self._is_price_like_layout_line(text)
            # Wide, non-price headers are treated as shared lines.
            if width >= (page_w * 0.74) and not is_price and len(text.split()) <= 16:
                shared_lines.append(line)
                continue
            if not is_price:
                anchor_lines.append(line)

        if len(anchor_lines) < 6:
            for line in body_lines:
                if line in shared_lines:
                    line["layout_role"] = "shared"
                    line["column_index"] = 0
                    line["column_bbox"] = [0.0, round(float(page_w), 1)]
                else:
                    line["layout_role"] = "body"
                    line["column_index"] = 1
                    line["column_bbox"] = [0.0, round(float(page_w), 1)]
            return lines

        entries = []
        for line in anchor_lines:
            b = line.get("bbox", [0, 0, 0, 0])
            entries.append((float(b[0]), float(b[2]), line))
        entries.sort(key=lambda e: e[0])

        gap_threshold = max(24.0, page_w * 0.03, median_h * 2.4)
        groups: List[List[Tuple[float, float, Dict]]] = []
        current: List[Tuple[float, float, Dict]] = [entries[0]]
        for ent in entries[1:]:
            if (ent[0] - current[-1][0]) <= gap_threshold:
                current.append(ent)
            else:
                groups.append(current)
                current = [ent]
        groups.append(current)

        def group_center(group: List[Tuple[float, float, Dict]]) -> float:
            return sum(e[0] for e in group) / max(len(group), 1)

        min_group_size = max(5, int(len(entries) * 0.12))
        changed = True
        while changed and len(groups) > 1:
            changed = False
            for idx, group in enumerate(list(groups)):
                if len(group) >= min_group_size:
                    continue
                if len(groups) == 2 and len(group) >= 2:
                    continue
                if idx == 0:
                    merge_idx = 1
                elif idx == (len(groups) - 1):
                    merge_idx = idx - 1
                else:
                    left_dist = abs(group_center(group) - group_center(groups[idx - 1]))
                    right_dist = abs(group_center(group) - group_center(groups[idx + 1]))
                    merge_idx = (idx - 1) if left_dist <= right_dist else (idx + 1)
                groups[merge_idx].extend(group)
                groups.pop(idx)
                changed = True
                break

        groups = [sorted(group, key=lambda e: e[0]) for group in groups]
        groups.sort(key=group_center)
        while len(groups) > 4:
            best_idx = 0
            best_gap = None
            for idx in range(len(groups) - 1):
                gap = abs(group_center(groups[idx + 1]) - group_center(groups[idx]))
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best_idx = idx
            groups[best_idx].extend(groups[best_idx + 1])
            groups.pop(best_idx + 1)

        columns: List[Dict[str, Any]] = []
        for group in groups:
            x0s = [e[0] for e in group]
            x1s = [e[1] for e in group]
            centers = [((e[0] + e[1]) / 2.0) for e in group]
            col = {
                "x0": float(np.percentile(x0s, 15)),
                "x1": float(np.percentile(x1s, 90)),
                "center": float(np.median(centers)),
                "anchors": [e[2] for e in group],
            }
            columns.append(col)
        columns.sort(key=lambda c: c["center"])

        # Defensive merge: collapse narrow, numeric-heavy columns into previous column.
        merged_columns: List[Dict[str, Any]] = []
        for col in columns:
            anchors = col.get("anchors", [])
            col_w = max(1.0, float(col["x1"]) - float(col["x0"]))
            price_ratio = self._price_like_ratio(anchors)
            if merged_columns and col_w < (page_w * 0.14) and price_ratio >= 0.6:
                prev = merged_columns[-1]
                prev["anchors"].extend(anchors)
                prev["x0"] = min(float(prev["x0"]), float(col["x0"]))
                prev["x1"] = max(float(prev["x1"]), float(col["x1"]))
                prev["center"] = (float(prev["x0"]) + float(prev["x1"])) / 2.0
            else:
                merged_columns.append(col)
        if merged_columns:
            columns = merged_columns

        # Merge tiny middle columns that are mostly continuation fragments.
        idx = 1
        while len(columns) > 2 and idx < (len(columns) - 1):
            col = columns[idx]
            anchors = col.get("anchors", [])
            if len(anchors) <= 6 and self._price_like_ratio(anchors) < 0.08:
                left_dist = abs(float(col["center"]) - float(columns[idx - 1]["center"]))
                right_dist = abs(float(col["center"]) - float(columns[idx + 1]["center"]))
                merge_idx = (idx - 1) if left_dist <= right_dist else (idx + 1)
                columns[merge_idx]["anchors"].extend(anchors)
                columns[merge_idx]["x0"] = min(float(columns[merge_idx]["x0"]), float(col["x0"]))
                columns[merge_idx]["x1"] = max(float(columns[merge_idx]["x1"]), float(col["x1"]))
                columns[merge_idx]["center"] = (float(columns[merge_idx]["x0"]) + float(columns[merge_idx]["x1"])) / 2.0
                columns.pop(idx)
                if idx > 1:
                    idx -= 1
                continue
            idx += 1

        # Merge very small residual columns into the nearest neighbor.
        min_col_anchors = max(5, int(len(anchor_lines) * 0.14))
        def weak_column(col: Dict[str, Any]) -> bool:
            anchors = col.get("anchors", [])
            if len(anchors) > 7:
                return False
            texts = [str(a.get("text") or "").strip() for a in anchors if isinstance(a, dict)]
            texts = [t for t in texts if t]
            if not texts:
                return True
            avg_chars = sum(len(re.sub(r"\s+", "", t)) for t in texts) / max(len(texts), 1)
            avg_words = sum(len(t.split()) for t in texts) / max(len(texts), 1)
            alpha_rich = sum(1 for t in texts if re.search(r"[A-Za-z]{3,}", t))
            mostly_numeric = sum(1 for t in texts if self._is_price_like_layout_line(t))
            if mostly_numeric >= len(texts) - 1:
                return True
            return avg_chars <= 10.0 and avg_words <= 2.2 and alpha_rich <= max(2, int(len(texts) * 0.5))

        changed = True
        while changed and len(columns) > 1:
            changed = False
            for idx, col in enumerate(list(columns)):
                anchors = col.get("anchors", [])
                is_weak = weak_column(col)
                if len(anchors) >= min_col_anchors and not is_weak:
                    continue
                if len(columns) == 2 and len(anchors) >= 4 and not is_weak:
                    continue
                if idx == 0:
                    merge_idx = 1
                elif idx == (len(columns) - 1):
                    merge_idx = idx - 1
                else:
                    left_dist = abs(float(col["center"]) - float(columns[idx - 1]["center"]))
                    right_dist = abs(float(col["center"]) - float(columns[idx + 1]["center"]))
                    merge_idx = (idx - 1) if left_dist <= right_dist else (idx + 1)
                columns[merge_idx]["anchors"].extend(anchors)
                columns[merge_idx]["x0"] = min(float(columns[merge_idx]["x0"]), float(col["x0"]))
                columns[merge_idx]["x1"] = max(float(columns[merge_idx]["x1"]), float(col["x1"]))
                columns[merge_idx]["center"] = (float(columns[merge_idx]["x0"]) + float(columns[merge_idx]["x1"])) / 2.0
                columns.pop(idx)
                changed = True
                break

        if len(columns) <= 1:
            for line in body_lines:
                if line in shared_lines:
                    line["layout_role"] = "shared"
                    line["column_index"] = 0
                    line["column_bbox"] = [0.0, round(float(page_w), 1)]
                else:
                    line["layout_role"] = "body"
                    line["column_index"] = 1
                    line["column_bbox"] = [0.0, round(float(page_w), 1)]
            return lines

        # Pre-assign non-price lines to nearest column center.
        preassigned: Dict[int, int] = {}
        for line in body_lines:
            if line in shared_lines:
                continue
            text = (line.get("text") or "").strip()
            if self._is_price_like_layout_line(text):
                continue
            b = line.get("bbox", [0, 0, 0, 0])
            cx = (float(b[0]) + float(b[2])) / 2.0
            nearest_idx = min(
                range(len(columns)),
                key=lambda idx: abs(cx - float(columns[idx]["center"])),
            )
            preassigned[id(line)] = int(nearest_idx + 1)

        row_tol = max(8.0, median_h * 1.35)
        max_row_gap = max(12.0, page_w * 0.12)

        for line in body_lines:
            if line in shared_lines:
                line["layout_role"] = "shared"
                line["column_index"] = 0
                line["column_bbox"] = [0.0, round(float(page_w), 1)]
                continue

            b = line.get("bbox", [0, 0, 0, 0])
            x0, y0, x1, y1 = [float(v) for v in b]
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            text = (line.get("text") or "").strip()
            col_idx = preassigned.get(id(line))

            if col_idx is None and self._is_price_like_layout_line(text):
                best_col = None
                best_score = None
                for other in body_lines:
                    if other is line:
                        continue
                    other_col = preassigned.get(id(other))
                    if other_col is None:
                        continue
                    ob = other.get("bbox", [0, 0, 0, 0])
                    ox0, oy0, ox1, oy1 = [float(v) for v in ob]
                    ocy = (oy0 + oy1) / 2.0
                    if abs(ocy - cy) > row_tol:
                        continue
                    if ox1 > (x0 + max_row_gap):
                        continue
                    dx = max(0.0, x0 - ox1)
                    score = dx + (abs(ocy - cy) * 0.25)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_col = other_col
                if best_col is not None:
                    col_idx = int(best_col)

            if col_idx is None:
                nearest_idx = min(
                    range(len(columns)),
                    key=lambda idx: abs(cx - float(columns[idx]["center"])),
                )
                col_idx = int(nearest_idx + 1)

            col = columns[max(0, min(len(columns) - 1, col_idx - 1))]
            line["layout_role"] = "body"
            line["column_index"] = int(col_idx)
            line["column_bbox"] = [round(float(col["x0"]), 1), round(float(col["x1"]), 1)]

        return lines

    def annotate_menu_raw_layout(self, menu_raw: Dict) -> Dict:
        if not isinstance(menu_raw, dict):
            return menu_raw
        pages = menu_raw.get("pages", [])
        if not isinstance(pages, list):
            return menu_raw
        for page in pages:
            if not isinstance(page, dict):
                continue
            lines = page.get("lines", [])
            if not isinstance(lines, list) or not lines:
                continue
            self._annotate_layout_columns(lines)
        return menu_raw

    def build_layout_aware_raw_text(self, menu_raw: Dict, fallback_raw_text: str = "") -> str:
        pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
        if not pages:
            return (fallback_raw_text or "").strip()

        chunks: List[str] = []
        for page in pages:
            lines = page.get("lines", []) if isinstance(page, dict) else []
            if not lines:
                continue
            # Ensure columns are annotated even if raw JSON came from an older run.
            self._annotate_layout_columns(lines)
            page_no = page.get("page")
            page_no_text = page_no if page_no is not None else "?"

            lines_sorted = sorted(
                lines, key=lambda l: (l.get("bbox", [0, 0, 0, 0])[1], l.get("bbox", [0, 0, 0, 0])[0])
            )
            page_w = max((l.get("bbox", [0, 0, 0, 0])[2] for l in lines_sorted), default=0.0)
            page_h = max((l.get("bbox", [0, 0, 0, 0])[3] for l in lines_sorted), default=0.0)

            body_lines: List[Dict] = []
            footer_lines: List[Dict] = []
            shared_lines: List[Dict] = []
            col_candidates: List[Dict] = []

            for line in lines_sorted:
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                box = line.get("bbox", [0, 0, 0, 0])
                x0, y0, x1, y1 = box
                width = max(0.0, x1 - x0)
                role = str(line.get("layout_role") or "").strip().lower()
                if role == "footer":
                    footer_lines.append(line)
                    continue
                is_footer = self._is_footer_text(text)
                if not is_footer and page_h > 0 and y0 >= (page_h * 0.65) and self._looks_like_legend_line(text):
                    is_footer = True
                if is_footer:
                    footer_lines.append(line)
                    continue
                body_lines.append(line)
                if role == "shared":
                    shared_lines.append(line)
                    continue
                if page_w > 0 and width >= (page_w * 0.62) and not self._is_price_like_layout_line(text):
                    shared_lines.append(line)
                else:
                    col_candidates.append(line)

            explicit_shared, explicit_columns, explicit_footer = self._collect_explicit_columns(lines_sorted)
            if explicit_footer:
                footer_lines.extend([l for l in explicit_footer if l not in footer_lines])

            if explicit_columns:
                columns = [col for col in explicit_columns if col]
                if explicit_shared:
                    shared_lines = [l for l in explicit_shared if l not in footer_lines]
            else:
                columns = self._split_lines_into_columns(col_candidates, page_w)

            parts: List[str] = [f"[PAGE {page_no_text}]"]
            if shared_lines:
                parts.append("[SHARED]")
                for line in self._order_lines_for_seed(shared_lines):
                    t = (line.get("text") or "").strip()
                    if t:
                        parts.append(t)

            if columns and (len(columns) > 1 or shared_lines):
                for col_idx, col_lines in enumerate(columns, start=1):
                    if not col_lines:
                        continue
                    parts.append(f"[COLUMN {col_idx}]")
                    for line in self._order_lines_for_seed(col_lines):
                        t = (line.get("text") or "").strip()
                        if t:
                            parts.append(t)
            elif columns and len(columns) == 1:
                parts.append("[BODY]")
                for line in self._order_lines_for_seed(columns[0]):
                    t = (line.get("text") or "").strip()
                    if t:
                        parts.append(t)
            elif body_lines:
                parts.append("[BODY]")
                for line in self._order_lines_for_seed(body_lines):
                    t = (line.get("text") or "").strip()
                    if t:
                        parts.append(t)

            if footer_lines:
                parts.append("[FOOTER]")
                for line in self._order_lines_for_seed(footer_lines):
                    t = (line.get("text") or "").strip()
                    if t:
                        parts.append(t)

            chunks.append("\n".join(parts).strip())

        text_out = "\n\n".join([c for c in chunks if c]).strip()
        if text_out:
            return text_out
        return (fallback_raw_text or "").strip()

    def build_single_column_raw_text(self, menu_raw: Dict, fallback_raw_text: str = "") -> str:
        pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
        if not pages:
            return (fallback_raw_text or "").strip()
        # If layout annotations already indicate multiple columns, honor them.
        for page in pages:
            if not isinstance(page, dict):
                continue
            lines = page.get("lines", [])
            if not isinstance(lines, list) or not lines:
                continue
            self._annotate_layout_columns(lines)
            explicit_cols = {
                int(line.get("column_index"))
                for line in lines
                if isinstance(line, dict)
                and isinstance(line.get("column_index"), (int, float))
                and int(line.get("column_index")) > 0
            }
            if len(explicit_cols) >= 2:
                return self.build_layout_aware_raw_text(menu_raw, fallback_raw_text=fallback_raw_text)

        chunks: List[str] = []
        for page in pages:
            lines = page.get("lines", []) if isinstance(page, dict) else []
            if not lines:
                continue
            page_no = page.get("page")
            page_no_text = page_no if page_no is not None else "?"

            lines_sorted = sorted(
                lines, key=lambda l: (l.get("bbox", [0, 0, 0, 0])[1], l.get("bbox", [0, 0, 0, 0])[0])
            )
            page_h = max((l.get("bbox", [0, 0, 0, 0])[3] for l in lines_sorted), default=0.0)

            body_lines: List[Dict] = []
            footer_lines: List[Dict] = []

            for line in lines_sorted:
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                box = line.get("bbox", [0, 0, 0, 0])
                y0 = box[1]
                is_footer = self._is_footer_text(text)
                if not is_footer and page_h > 0 and y0 >= (page_h * 0.65) and self._looks_like_legend_line(text):
                    is_footer = True
                if is_footer:
                    footer_lines.append(line)
                else:
                    body_lines.append(line)

            parts: List[str] = [f"[PAGE {page_no_text}]"]
            if body_lines:
                parts.append("[BODY]")
                for line in self._order_lines_for_seed(body_lines):
                    t = (line.get("text") or "").strip()
                    if t:
                        parts.append(t)
            if footer_lines:
                parts.append("[FOOTER]")
                for line in self._order_lines_for_seed(footer_lines):
                    t = (line.get("text") or "").strip()
                    if t:
                        parts.append(t)

            chunks.append("\n".join(parts).strip())

        text_out = "\n\n".join([c for c in chunks if c]).strip()
        if text_out:
            return text_out
        return (fallback_raw_text or "").strip()

    def build_row_column_details(self, menu_raw: Dict) -> Dict[str, Any]:
        pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
        out_pages: List[Dict[str, Any]] = []
        for page in pages:
            lines = page.get("lines", []) if isinstance(page, dict) else []
            if not lines:
                continue
            self._annotate_layout_columns(lines)
            page_no = page.get("page")
            lines_sorted = sorted(
                lines, key=lambda l: (l.get("bbox", [0, 0, 0, 0])[1], l.get("bbox", [0, 0, 0, 0])[0])
            )
            page_w = max((l.get("bbox", [0, 0, 0, 0])[2] for l in lines_sorted), default=0.0)
            page_h = max((l.get("bbox", [0, 0, 0, 0])[3] for l in lines_sorted), default=0.0)

            shared_lines: List[Dict] = []
            col_candidates: List[Dict] = []
            footer_lines: List[Dict] = []
            col_map: Dict[int, List[Dict]] = {}

            for line in lines_sorted:
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                box = line.get("bbox", [0, 0, 0, 0])
                x0, y0, x1, y1 = box
                width = max(0.0, x1 - x0)
                role = str(line.get("layout_role") or "").strip().lower()
                col_raw = line.get("column_index")
                col_idx = None
                if isinstance(col_raw, (int, float)):
                    try:
                        col_idx = int(col_raw)
                    except Exception:
                        col_idx = None

                if role == "footer":
                    footer_lines.append(line)
                    continue
                is_footer = self._is_footer_text(text)
                if not is_footer and page_h > 0 and y0 >= (page_h * 0.65) and self._looks_like_legend_line(text):
                    is_footer = True
                if is_footer:
                    footer_lines.append(line)
                    continue
                if role == "shared":
                    shared_lines.append(line)
                    continue
                if col_idx is not None and col_idx > 0:
                    col_map.setdefault(col_idx, []).append(line)
                    continue
                if page_w > 0 and width >= (page_w * 0.62):
                    shared_lines.append(line)
                else:
                    col_candidates.append(line)

            if col_map:
                columns = [col_map[k] for k in sorted(col_map.keys())]
                if col_candidates:
                    # Attach unassigned candidate lines to the nearest explicit column.
                    col_centers: Dict[int, float] = {}
                    for key in sorted(col_map.keys()):
                        cx_vals = [
                            ((l.get("bbox", [0, 0, 0, 0])[0] + l.get("bbox", [0, 0, 0, 0])[2]) / 2.0)
                            for l in col_map[key]
                        ]
                        if cx_vals:
                            col_centers[key] = sum(cx_vals) / len(cx_vals)
                    for line in col_candidates:
                        b = line.get("bbox", [0, 0, 0, 0])
                        cx = (b[0] + b[2]) / 2.0
                        if not col_centers:
                            col_map.setdefault(1, []).append(line)
                            continue
                        nearest = min(col_centers.keys(), key=lambda k: abs(cx - col_centers[k]))
                        col_map.setdefault(nearest, []).append(line)
                    columns = [col_map[k] for k in sorted(col_map.keys())]
            else:
                columns = self._split_lines_into_columns(col_candidates, page_w)
                if not columns and col_candidates:
                    columns = [col_candidates]

            page_obj: Dict[str, Any] = {
                "page": page_no,
                "width": round(float(page_w), 1),
                "height": round(float(page_h), 1),
                "shared_rows": self._group_lines_to_rows(shared_lines),
                "columns": [],
                "footer_rows": self._group_lines_to_rows(footer_lines),
            }
            for col_idx, col_lines in enumerate(columns, start=1):
                col_bbox = None
                if col_lines:
                    x0s = [float(l.get("bbox", [0, 0, 0, 0])[0]) for l in col_lines]
                    y0s = [float(l.get("bbox", [0, 0, 0, 0])[1]) for l in col_lines]
                    x1s = [float(l.get("bbox", [0, 0, 0, 0])[2]) for l in col_lines]
                    y1s = [float(l.get("bbox", [0, 0, 0, 0])[3]) for l in col_lines]
                    if x0s and y0s and x1s and y1s:
                        col_bbox = [
                            round(min(x0s), 1),
                            round(min(y0s), 1),
                            round(max(x1s), 1),
                            round(max(y1s), 1),
                        ]
                page_obj["columns"].append(
                    {
                        "column": col_idx,
                        "bbox": col_bbox,
                        "rows": self._group_lines_to_rows(col_lines),
                    }
                )
            out_pages.append(page_obj)
        return {"pages": out_pages}

    def build_layout_lines(self, menu_raw: Dict) -> Dict[str, Any]:
        pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
        out_pages: List[Dict[str, Any]] = []
        for page in pages:
            lines = page.get("lines", []) if isinstance(page, dict) else []
            if not lines:
                continue
            self._annotate_layout_columns(lines)
            page_no = page.get("page")
            boxes = [l.get("bbox", [0, 0, 0, 0]) for l in lines if isinstance(l, dict)]
            page_w = max((float(b[2]) for b in boxes), default=0.0)
            page_h = max((float(b[3]) for b in boxes), default=0.0)

            out_lines: List[Dict[str, Any]] = []
            for line in lines:
                if not isinstance(line, dict):
                    continue
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                b = line.get("bbox", [0, 0, 0, 0])
                try:
                    x0, y0, x1, y1 = [float(v) for v in b]
                except Exception:
                    x0 = y0 = x1 = y1 = 0.0
                cx = (x0 + x1) / 2.0
                cy = (y0 + y1) / 2.0
                nbbox = None
                if page_w > 0 and page_h > 0:
                    nbbox = [
                        round(x0 / page_w, 4),
                        round(y0 / page_h, 4),
                        round(x1 / page_w, 4),
                        round(y1 / page_h, 4),
                    ]
                out_line = {
                    "text": text,
                    "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                    "center": [round(cx, 1), round(cy, 1)],
                    "nbbox": nbbox,
                    "column_index": (
                        int(line.get("column_index"))
                        if isinstance(line.get("column_index"), (int, float))
                        else None
                    ),
                    "layout_role": str(line.get("layout_role") or "") or None,
                    "column_bbox": (
                        [
                            round(float(line.get("column_bbox")[0]), 1),
                            round(float(line.get("column_bbox")[1]), 1),
                        ]
                        if isinstance(line.get("column_bbox"), (list, tuple))
                        and len(line.get("column_bbox")) >= 2
                        else None
                    ),
                }
                icons: List[str] = []
                raw_icons = line.get("icons") if isinstance(line, dict) else None
                if isinstance(raw_icons, list):
                    for icon in raw_icons:
                        val = str(icon or "").strip().lower()
                        if val and val not in icons:
                            icons.append(val)
                if icons:
                    out_line["icons"] = icons
                try:
                    pg_no = int(page_no)
                except Exception:
                    pg_no = 0
                out_line["line_id"] = f"p{pg_no:03d}_l{(len(out_lines) + 1):04d}"
                out_lines.append(out_line)

            out_pages.append(
                {
                    "page": page_no,
                    "width": round(page_w, 1),
                    "height": round(page_h, 1),
                    "lines": out_lines,
                }
            )
        return {"pages": out_pages}

    def detect_real_two_columns(self, menu_raw: Dict) -> bool:
        pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
        for page in pages:
            lines = page.get("lines", []) if isinstance(page, dict) else []
            if not lines:
                continue
            self._annotate_layout_columns(lines)
            explicit_cols = {
                int(line.get("column_index"))
                for line in lines
                if isinstance(line, dict)
                and isinstance(line.get("column_index"), (int, float))
                and int(line.get("column_index")) > 0
            }
            if len(explicit_cols) >= 2:
                return True
            page_w = max((l.get("bbox", [0, 0, 0, 0])[2] for l in lines), default=0.0)
            page_h = max((l.get("bbox", [0, 0, 0, 0])[3] for l in lines), default=0.0)
            if page_w <= 0:
                continue

            body_lines: List[Dict] = []
            col_candidates: List[Dict] = []
            for line in lines:
                if not isinstance(line, dict):
                    continue
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                box = line.get("bbox", [0, 0, 0, 0])
                x0, y0, x1, y1 = box
                width = max(0.0, x1 - x0)
                is_footer = self._is_footer_text(text)
                if not is_footer and page_h > 0 and y0 >= (page_h * 0.65) and self._looks_like_legend_line(text):
                    is_footer = True
                if is_footer:
                    continue
                body_lines.append(line)
                if width < (page_w * 0.62):
                    col_candidates.append(line)

            if len(col_candidates) < 10:
                continue

            columns = self._split_lines_into_columns(col_candidates, page_w)
            if not columns or len(columns) < 2:
                continue

            col_stats = []
            for col in columns:
                if not col:
                    continue
                xs = []
                x0s = []
                x1s = []
                for l in col:
                    b = l.get("bbox", [0, 0, 0, 0])
                    xs.append((b[0] + b[2]) / 2.0)
                    x0s.append(b[0])
                    x1s.append(b[2])
                if not xs:
                    continue
                col_stats.append(
                    {
                        "lines": col,
                        "xc": sum(xs) / len(xs),
                        "x0": min(x0s),
                        "x1": max(x1s),
                    }
                )

            if len(col_stats) < 2:
                continue

            col_stats.sort(key=lambda c: c["xc"])
            left = col_stats[0]
            right = col_stats[1]
            balance = min(len(left["lines"]), len(right["lines"])) / max(len(left["lines"]), len(right["lines"]))
            if balance < 0.25:
                continue

            left_x1s = [l.get("bbox", [0, 0, 0, 0])[2] for l in left["lines"]]
            right_x0s = [l.get("bbox", [0, 0, 0, 0])[0] for l in right["lines"]]
            right_x1s = [l.get("bbox", [0, 0, 0, 0])[2] for l in right["lines"]]
            if not left_x1s or not right_x0s or not right_x1s:
                continue
            left_x1 = float(np.percentile(left_x1s, 85))
            right_x0 = float(np.percentile(right_x0s, 15))
            gap = right_x0 - left_x1
            if gap < (page_w * 0.04):
                continue

            right_x1 = float(np.percentile(right_x1s, 85))
            right_w = right_x1 - right_x0
            if right_w < (page_w * 0.38):
                price_ratio = self._price_like_ratio(right["lines"])
                if right_w < (page_w * 0.25) and price_ratio >= 0.3:
                    continue
                if price_ratio >= 0.45:
                    continue

            return True

        return False

    def _price_like_ratio(self, lines: List[Dict]) -> float:
        tokens: List[str] = []
        for line in lines:
            text = (line.get("text") or "").strip()
            if not text:
                continue
            tokens.extend([t for t in re.split(r"\s+", text) if t])
        if not tokens:
            return 0.0
        price_like = 0
        for tok in tokens:
            t = tok.lower().strip()
            if any(sym in t for sym in ("Ã¢â€šÂ¹", "inr", "rs", "aed", "sar", "$")):
                price_like += 1
                continue
            t = t.replace(",", "").replace(".", "")
            if t.isdigit():
                price_like += 1
        return price_like / max(len(tokens), 1)

    def _group_lines_to_rows(self, lines: List[Dict]) -> List[Dict[str, Any]]:
        if not lines:
            return []
        sorted_lines = sorted(lines, key=lambda l: (l.get("bbox", [0, 0, 0, 0])[1], l.get("bbox", [0, 0, 0, 0])[0]))
        heights = []
        for line in sorted_lines:
            b = line.get("bbox", [0, 0, 0, 0])
            h = float(b[3] - b[1])
            if h > 0:
                heights.append(h)
        median_h = float(np.median(heights)) if heights else 20.0
        row_tol = max(6.0, median_h * 0.55)

        rows: List[List[Dict]] = []
        centers: List[float] = []
        for line in sorted_lines:
            b = line.get("bbox", [0, 0, 0, 0])
            cy = (b[1] + b[3]) / 2.0
            if not rows or abs(cy - centers[-1]) > row_tol:
                rows.append([line])
                centers.append(cy)
            else:
                rows[-1].append(line)
                centers[-1] = (centers[-1] + cy) / 2.0

        out_rows: List[Dict[str, Any]] = []
        for idx, row in enumerate(rows, start=1):
            row_sorted = sorted(row, key=lambda l: l.get("bbox", [0, 0, 0, 0])[0])
            entries = []
            texts = []
            for line in row_sorted:
                b = line.get("bbox", [0, 0, 0, 0])
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                texts.append(text)
                entries.append(
                    {
                        "x": round(float(b[0]), 1),
                        "y": round(float(b[1]), 1),
                        "text": text,
                        "bbox": [float(b[0]), float(b[1]), float(b[2]), float(b[3])],
                    }
                )
            if not entries:
                continue
            y_avg = sum(e["y"] for e in entries) / len(entries)
            out_rows.append(
                {
                    "row": idx,
                    "y": round(float(y_avg), 1),
                    "text": " | ".join(texts),
                    "entries": entries,
                }
            )
        return out_rows

    def _split_lines_into_columns(self, lines: List[Dict], page_w: float) -> List[List[Dict]]:
        if not lines:
            return []
        if page_w <= 0:
            return [lines]
        if len(lines) < 10:
            return [lines]

        centers = []
        for line in lines:
            b = line.get("bbox", [0, 0, 0, 0])
            centers.append(((b[0] + b[2]) / 2.0, line))
        xs = [c[0] for c in centers]
        c1 = float(np.percentile(xs, 30))
        c2 = float(np.percentile(xs, 70))
        if c1 > c2:
            c1, c2 = c2, c1
        for _ in range(8):
            g1 = [x for x in xs if abs(x - c1) <= abs(x - c2)]
            g2 = [x for x in xs if abs(x - c2) < abs(x - c1)]
            if g1:
                c1 = sum(g1) / len(g1)
            if g2:
                c2 = sum(g2) / len(g2)
        if c1 > c2:
            c1, c2 = c2, c1

        gap = c2 - c1
        if gap < (page_w * 0.22):
            return [lines]
        mid = (c1 + c2) / 2.0

        left: List[Dict] = []
        right: List[Dict] = []
        for x, line in centers:
            if x <= mid:
                left.append(line)
            else:
                right.append(line)

        if not left or not right:
            return [lines]
        balance = min(len(left), len(right)) / max(len(left), len(right))
        if balance < 0.18:
            return [lines]

        return [left, right]

    def format_raw_text_with_openai_result(
        self,
        raw_text: str,
        deterministic: Dict | None = None,
        row_column_details: Dict[str, Any] | None = None,
        layout_lines: Dict[str, Any] | None = None,
        icon_lines: List[Dict[str, Any]] | None = None,
        icon_detections: List[Dict[str, Any]] | None = None,
    ) -> Dict:
        fallback = deterministic or {
            "menu_name": None,
            "items": [],
            "other_text": [],
            "footer_text": [],
            "notes": [],
        }
        raw_text = (raw_text or "").strip()
        if not raw_text:
            return {
                "formatted": fallback,
                "openai_raw": None,
                "openai_parsed": None,
                "source": "deterministic",
                "error": "Google OCR raw text is empty",
            }

        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        if not api_key:
            return {
                "formatted": fallback,
                "openai_raw": None,
                "openai_parsed": None,
                "source": "deterministic",
                "error": "OPENAI_API_KEY not set",
            }

        has_icon_lines = isinstance(icon_lines, list) and len(icon_lines) > 0
        has_icon_detections = isinstance(icon_detections, list) and len(icon_detections) > 0
        client = OpenAI(api_key=api_key)
        system = (
            "You are a menu formatter. "
            "The input is raw OCR text from Google Document AI. "
            "This raw OCR text can be noisy, fragmented, and in random order. "
            "Convert this raw text into a structured JSON menu. "
            "Output JSON only with keys: menu_name, items, other_text, footer_text, notes. "
            "Optional top-level key allowed: extra_sections (object/array) only when explicitly present in the menu text. "
            "Each item must include: name, price, kcal, description, allergens, veg, non_veg, page, dish_type, timings. "
            "Optional item key allowed: extra_attributes (object) for explicit non-standard item metadata (e.g., spicy level, chef special markers). "
            "If a dish has an explicit short marker in brackets like (VG), (V), or (S), preserve it in "
            "item.extra_attributes.dietary_marker exactly as shown (without brackets). "
            "Treat the OCR text as noisy raw text and infer structure carefully. "
            "If the text appears mismatched or interleaved, still try to infer the most likely menu structure "
            "using context and proximity (names near descriptions/prices), and do not drop dish names. "
            "If sections like [COLUMN 1]/[COLUMN 2] are present, parse each column independently first. "
            "When [COLUMN N] markers are present, treat each column block as an independent cropped-layout region "
            "and preserve within-column reading order before combining columns. "
            "Never carry a section/shared price across different [COLUMN N] blocks unless explicitly repeated. "
            "You must properly assign each description, price, and kcal to the correct dish name. "
            "Do not miss any dish name present in the input. "
            "Coverage rule: every non-empty input line must be mapped either to an item field "
            "(name/description/price/kcal/dish_type/timings) or to other_text/footer_text/notes. "
            "If uncertain, keep the line in other_text instead of dropping it. "
            "Do not hallucinate fields that are not supported by text. "
            "Use null/empty values when uncertain. "
            "Keep legal/disclaimer/legend text in footer_text, not in descriptions. "
            "If allergen or veg/non_veg is not explicit in the raw text, keep them empty/null. "
            "If a page heading indicates dish type (e.g., breakfast/lunch/dinner/soups), set dish_type for all dishes from that page. "
            "If a heading appears directly above/before a group of dishes, use that heading text as dish_type for those dishes. "
            "Do not leave dish_type empty when such heading context exists. "
            "If no heading context is present for a dish, keep dish_type as null. "
            "If page timings are present (e.g., 6:30 AM TO 10:30 AM), set timings for all dishes from that page. "
            "If a single explicit price is shown and then multiple dish names follow without their own prices, "
            "carry that same price to subsequent dishes until a new explicit price appears. "
            "If multiple labeled prices are present for one dish (e.g., glass/bottle or small/medium/large), "
            "also include item.extra_attributes.price_options as an object mapping each label to its price. "
            "Set kcal only when explicitly marked by kcal/cal/calorie words; never infer kcal from plain price numbers. "
            "A line that is only a calorie value (e.g., '337 Kcal') is not a dish name; attach it to the nearest dish as kcal."
        )
        if has_icon_lines:
            system += (
                " Icon annotations are also provided as line-level text+icons. "
                "Use these icon labels as the source of allergens and veg/non_veg flags. "
                "Allergens must come only from icon labels (exclude veg/non_veg from allergens). "
                "Only apply icon labels to the item whose line text matches that icon line."
            )
        if has_icon_detections:
            system += (
                " Icon detections with label and bounding boxes are also provided. "
                "Use icon spatial proximity to lines/items for icon assignment when useful."
            )
        has_row_col = (
            isinstance(row_column_details, dict)
            and isinstance(row_column_details.get("pages"), list)
            and len(row_column_details.get("pages")) > 0
        )
        has_layout_lines = (
            isinstance(layout_lines, dict)
            and isinstance(layout_lines.get("pages"), list)
            and len(layout_lines.get("pages")) > 0
        )
        if has_row_col:
            system += (
                " Row/column details are also provided; use them as authoritative layout signals for mapping "
                "prices and descriptions to the right dish."
            )
        if has_layout_lines:
            system += (
                " Line-level layout with bounding boxes (absolute and normalized) is provided; use it to resolve "
                "reading order and to associate prices/descriptions with the correct dish based on proximity."
            )
        user = {
            "source": "google_document_ai_raw_text",
            "instruction": (
                "This is raw OCR text in random order. "
                "Understand it properly, map each description/price/kcal to the right dish name, "
                "and do not miss any dish name. "
                "Do not drop any non-empty line; map unclassified lines to other_text/footer_text/notes."
            ),
            "raw_text": raw_text,
        }
        if has_icon_lines:
            user["icon_lines"] = icon_lines
        if has_icon_detections:
            user["icon_detections"] = icon_detections
        if has_row_col:
            user["row_column_details"] = row_column_details
        if has_layout_lines:
            user["layout_lines"] = layout_lines

        def call_openai(payload: Dict[str, Any], timeout_sec: int):
            return client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload)},
                ],
                timeout=timeout_sec,
                temperature=0.2,
            )

        base_timeout = max(int(self.config.openai_timeout), 60)
        light_user = dict(user)
        light_user.pop("icon_lines", None)
        light_user.pop("icon_detections", None)
        light_user.pop("row_column_details", None)
        light_user.pop("layout_lines", None)
        compact_raw_text = self._compact_openai_raw_text(raw_text)
        compact_user = dict(light_user)
        if compact_raw_text:
            compact_user["raw_text"] = compact_raw_text

        attempts: List[Tuple[Dict[str, Any], int]] = [
            (user, base_timeout),
            (user, max(base_timeout, 120)),
            (light_user, max(base_timeout, 150)),
        ]
        if compact_raw_text and compact_raw_text != raw_text:
            attempts.append((compact_user, max(base_timeout, 210)))

        chunked_fallback_applied = False
        chunked_page_fallback_applied = False
        chunked_primary_applied = False
        response = None
        last_exc: Exception | None = None
        parsed: Dict[str, Any] | None = None
        raw_resp = ""

        raw_pages_map = self._split_raw_text_pages(raw_text)
        raw_pages = sorted(int(p) for p in raw_pages_map.keys())
        force_page_by_page = bool(self.config.openai_page_by_page)
        if force_page_by_page:
            prefer_chunked_primary = len(raw_pages) > 1
            primary_chunk_pages = 1
        else:
            prefer_chunked_primary = len(raw_pages) > 5
            primary_chunk_pages = 5

        def parsed_pages(parsed_obj: Dict[str, Any] | None) -> set[int]:
            out: set[int] = set()
            if not isinstance(parsed_obj, dict):
                return out
            for it in (parsed_obj.get("items") or []):
                if not isinstance(it, dict):
                    continue
                try:
                    out.add(int(it.get("page")))
                except Exception:
                    continue
            return out

        def has_missing_pages(parsed_obj: Dict[str, Any] | None) -> bool:
            if not raw_pages:
                return False
            got = parsed_pages(parsed_obj)
            return not set(raw_pages).issubset(got)

        if prefer_chunked_primary:
            chunked_parsed = self._run_chunked_openai_fallback(
                raw_text=raw_text,
                row_column_details=row_column_details,
                layout_lines=layout_lines,
                icon_lines=icon_lines,
                icon_detections=icon_detections,
                call_openai=call_openai,
                base_timeout=base_timeout,
                max_pages_per_chunk=primary_chunk_pages,
            )
            if isinstance(chunked_parsed, dict) and has_missing_pages(chunked_parsed) and primary_chunk_pages > 1:
                # If any page is missing after grouped chunks, retry page-by-page to recover coverage.
                page_parsed = self._run_chunked_openai_fallback(
                    raw_text=raw_text,
                    row_column_details=row_column_details,
                    layout_lines=layout_lines,
                    icon_lines=icon_lines,
                    icon_detections=icon_detections,
                    call_openai=call_openai,
                    base_timeout=base_timeout,
                    max_pages_per_chunk=1,
                )
                if isinstance(page_parsed, dict):
                    chunked_parsed = page_parsed
                    chunked_page_fallback_applied = True
            if isinstance(chunked_parsed, dict):
                parsed = chunked_parsed
                raw_resp = json.dumps(parsed, indent=2, ensure_ascii=False)
                chunked_fallback_applied = True
                chunked_primary_applied = True
            else:
                return {
                    "formatted": fallback,
                    "openai_raw": None,
                    "openai_parsed": None,
                    "source": "deterministic",
                    "error": "OpenAI chunked parsing failed",
                }

        for payload, timeout_sec in attempts:
            if prefer_chunked_primary:
                break
            try:
                response = call_openai(payload, timeout_sec)
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                retryable = (
                    isinstance(exc, (APITimeoutError, APIConnectionError))
                    or "timed out" in msg
                    or "rate limit" in msg
                    or "temporarily unavailable" in msg
                )
                fatal = any(k in msg for k in ("incorrect api key", "authentication", "permission denied"))
                if fatal:
                    break
                if not retryable:
                    continue

        if (not prefer_chunked_primary) and response is None:
            chunked_parsed = self._run_chunked_openai_fallback(
                raw_text=raw_text,
                row_column_details=row_column_details,
                layout_lines=layout_lines,
                icon_lines=icon_lines,
                icon_detections=icon_detections,
                call_openai=call_openai,
                base_timeout=base_timeout,
                max_pages_per_chunk=5,
            )
            if not isinstance(chunked_parsed, dict):
                # Last-resort fallback: page-by-page parsing for long/noisy menus.
                chunked_parsed = self._run_chunked_openai_fallback(
                    raw_text=raw_text,
                    row_column_details=row_column_details,
                    layout_lines=layout_lines,
                    icon_lines=icon_lines,
                    icon_detections=icon_detections,
                    call_openai=call_openai,
                    base_timeout=base_timeout,
                    max_pages_per_chunk=1,
                )
                if isinstance(chunked_parsed, dict):
                    chunked_page_fallback_applied = True
            if not isinstance(chunked_parsed, dict):
                return {
                    "formatted": fallback,
                    "openai_raw": None,
                    "openai_parsed": None,
                    "source": "deterministic",
                    "error": str(last_exc) if last_exc else "OpenAI request failed",
                }
            parsed = chunked_parsed
            raw_resp = json.dumps(parsed, indent=2, ensure_ascii=False)
            chunked_fallback_applied = True
        else:
            if not prefer_chunked_primary:
                try:
                    raw_resp = response.output[0].content[0].text
                except Exception:
                    raw_resp = ""

                parsed = self._parse_json_maybe(raw_resp)
                if not isinstance(parsed, dict):
                    return {
                        "formatted": fallback,
                        "openai_raw": raw_resp,
                        "openai_parsed": None,
                        "source": "deterministic",
                        "error": "OpenAI returned invalid JSON",
                    }

        if not isinstance(parsed, dict):
            return {
                "formatted": fallback,
                "openai_raw": raw_resp if raw_resp else None,
                "openai_parsed": None,
                "source": "deterministic",
                "error": "OpenAI parsing produced no structured result",
            }

        dense_retry_applied = False
        raw_text_for_fixes = raw_text
        if self._should_retry_dense_calorie_parse(raw_text, parsed):
            dense_raw_text = self._rewrite_dense_calorie_raw_text(raw_text)
            if dense_raw_text and dense_raw_text != raw_text:
                dense_user = dict(user)
                dense_user["raw_text"] = dense_raw_text
                dense_user.pop("icon_lines", None)
                dense_user.pop("row_column_details", None)
                dense_user.pop("layout_lines", None)
                dense_timeout = max(int(self.config.openai_timeout), 120)
                try:
                    dense_response = call_openai(dense_user, dense_timeout)
                    dense_raw_resp = dense_response.output[0].content[0].text
                    dense_parsed = self._parse_json_maybe(dense_raw_resp)
                    if isinstance(dense_parsed, dict):
                        old_quality = self._openai_parsed_quality_score(parsed)
                        new_quality = self._openai_parsed_quality_score(dense_parsed)
                        if new_quality > (old_quality + 0.5):
                            parsed = dense_parsed
                            raw_resp = dense_raw_resp
                            raw_text_for_fixes = dense_raw_text
                            dense_retry_applied = True
                except Exception:
                    pass

        parsed, section_fix_applied = self._maybe_fix_shared_price_section_output(raw_text_for_fixes, parsed)
        beverage_dessert_fix_applied = False
        if self.config.enable_menu_specific_post_fixes and not section_fix_applied:
            parsed, beverage_dessert_fix_applied = self._maybe_fix_interleaved_beverage_dessert_output(raw_text_for_fixes, parsed)
        dense_section_fix_applied = False
        if self.config.enable_menu_specific_post_fixes and not section_fix_applied and not beverage_dessert_fix_applied:
            parsed, dense_section_fix_applied = self._maybe_fix_dense_menu_missing_sections(raw_text_for_fixes, parsed)
        legend_allergen_fix_applied = False
        parsed, legend_allergen_fix_applied = self._maybe_fix_legend_code_allergens(raw_text_for_fixes, parsed)
        dense_group_fix_applied = False
        if self.config.enable_menu_specific_post_fixes:
            parsed, dense_group_fix_applied = self._maybe_fix_dense_grouped_variants(raw_text_for_fixes, parsed)
        parsed, icon_type_fix_applied = self._coerce_parsed_icon_fields(
            parsed,
            icon_lines,
            raw_text=raw_text_for_fixes,
        )
        parsed, multi_price_fix_applied = self._maybe_fix_multi_price_columns(raw_text_for_fixes, parsed)
        parsed, dense_price_fix_applied = self._maybe_fix_suspicious_dense_prices(raw_text_for_fixes, parsed)
        parsed, dietary_marker_fix_applied = self._maybe_attach_dietary_markers(
            parsed,
            fallback,
            raw_text=raw_text_for_fixes,
        )
        heading_dish_type_fix_applied = False
        parsed, heading_dish_type_fix_applied = self._maybe_fill_dish_type_from_fallback(parsed, fallback)
        parsed, menu_name_fix_applied = self._maybe_fix_menu_name_from_fallback(parsed, fallback)
        if (
            section_fix_applied
            or beverage_dessert_fix_applied
            or dense_section_fix_applied
            or legend_allergen_fix_applied
            or dense_group_fix_applied
            or icon_type_fix_applied
            or multi_price_fix_applied
            or dense_price_fix_applied
            or dietary_marker_fix_applied
            or heading_dish_type_fix_applied
            or menu_name_fix_applied
        ):
            raw_resp = json.dumps(parsed, indent=2, ensure_ascii=False)

        formatted = self._normalize_menu_json(parsed, fallback)
        skip_align_for_dense_sections = (
            self.config.enable_menu_specific_post_fixes
            and self._looks_like_dense_calorie_raw_text(raw_text_for_fixes)
            and self._has_dense_special_sections(parsed)
        )
        if (
            has_icon_lines
            and
            not section_fix_applied
            and not beverage_dessert_fix_applied
            and not dense_section_fix_applied
            and not skip_align_for_dense_sections
        ):
            formatted = self._align_formatted_with_deterministic(formatted, fallback)
        # Always backfill missing core fields from deterministic extraction.
        # This avoids null prices when GPT misses a value even though OCR captured it.
        formatted = self._backfill_missing_item_fields_from_deterministic(formatted, fallback)
        formatted, missing_item_recovery_applied = self._recover_missing_items_from_deterministic(formatted, fallback)
        formatted, fragment_item_drop_applied = self._drop_likely_fragment_items(formatted)
        source = "openai_raw_text"
        if section_fix_applied:
            source = "openai_raw_text_section_fix"
        elif beverage_dessert_fix_applied:
            source = "openai_raw_text_beverage_dessert_fix"
        elif dense_section_fix_applied:
            source = "openai_raw_text_dense_section_fix"
        elif legend_allergen_fix_applied:
            source = "openai_raw_text_legend_allergen_fix"
        elif dense_group_fix_applied:
            source = "openai_raw_text_dense_group_fix"
        elif icon_type_fix_applied:
            source = "openai_raw_text_icon_field_fix"
        elif multi_price_fix_applied:
            source = "openai_raw_text_multi_price_fix"
        elif dense_price_fix_applied:
            source = "openai_raw_text_price_fix"
        elif dietary_marker_fix_applied:
            source = "openai_raw_text_dietary_marker_fix"
        elif heading_dish_type_fix_applied:
            source = "openai_raw_text_heading_dish_type_fix"
        elif menu_name_fix_applied:
            source = "openai_raw_text_menu_name_fix"
        elif missing_item_recovery_applied:
            source = "openai_raw_text_missing_item_recovery"
        elif fragment_item_drop_applied:
            source = "openai_raw_text_fragment_item_drop"
        elif chunked_page_fallback_applied:
            source = "openai_raw_text_chunked_page"
        elif chunked_primary_applied:
            source = "openai_raw_text_chunked_primary"
        elif chunked_fallback_applied:
            source = "openai_raw_text_chunked"
        elif dense_retry_applied:
            source = "openai_raw_text_dense_retry"
        elif not has_icon_lines:
            # No icon context: keep GPT's raw-text parse as primary output.
            source = "openai_raw_text_no_icons"
        cleaned_openai_raw = raw_resp
        try:
            cleaned_openai_raw = json.dumps(formatted, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return {
            "formatted": formatted,
            "openai_raw": cleaned_openai_raw,
            "openai_parsed": parsed,
            "source": source,
            "error": None,
        }

    def _align_formatted_with_deterministic(self, formatted: Dict, deterministic: Dict) -> Dict:
        if not isinstance(formatted, dict):
            return deterministic
        det_items = deterministic.get("items") or []
        out_items = formatted.get("items") or []
        if not isinstance(det_items, list) or not det_items:
            return formatted
        if not isinstance(out_items, list):
            formatted["items"] = [dict(it) for it in det_items]
            return formatted

        def norm_name(name: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", (name or "").lower())

        def is_kcal_only_name(name: str) -> bool:
            txt = re.sub(r"\s+", " ", str(name or "").strip().lower())
            if not txt:
                return False
            return (
                re.fullmatch(
                    r"[\W_]*\d{1,4}(?:[.,]\d{1,2})?\s*(kcal|cal|calorie|calories)\b[\W_]*",
                    txt,
                )
                is not None
            )

        def is_heading_only_item(item_obj: Dict[str, Any]) -> bool:
            name = re.sub(r"\s+", " ", str(item_obj.get("name") or "").strip())
            if not name:
                return False
            price = str(item_obj.get("price") or "").strip()
            kcal = str(item_obj.get("kcal") or "").strip()
            if price or kcal:
                return False
            if len(name.split()) > 3:
                return False
            alpha = [c for c in name if c.isalpha()]
            if not alpha:
                return False
            upper_ratio = sum(1 for c in alpha if c.isupper()) / max(len(alpha), 1)
            return upper_ratio >= 0.85

        cand_items = [it for it in out_items if isinstance(it, dict)]
        used_idx = set()
        merged_items: List[Dict] = []

        for det in det_items:
            base = dict(det)
            dname = str(base.get("name") or "")
            if is_kcal_only_name(dname):
                continue
            if is_heading_only_item(base):
                continue
            dkey = norm_name(dname)
            best_idx = None
            best_ratio = 0.0
            for idx, cand in enumerate(cand_items):
                if idx in used_idx:
                    continue
                ckey = norm_name(str(cand.get("name") or ""))
                if not ckey:
                    continue
                if dkey and ckey == dkey:
                    best_idx = idx
                    best_ratio = 1.0
                    break
                ratio = SequenceMatcher(None, dkey, ckey).ratio() if dkey else 0.0
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = idx
            if best_idx is not None and best_ratio >= 0.72:
                used_idx.add(best_idx)
                cand = cand_items[best_idx]
                desc = str(cand.get("description") or "").strip()
                if desc and not self._is_footer_text(desc):
                    base["description"] = desc
                if not base.get("allergens") and isinstance(cand.get("allergens"), list):
                    base["allergens"] = cand.get("allergens")
                if base.get("veg") is None and isinstance(cand.get("veg"), bool):
                    base["veg"] = cand.get("veg")
                if base.get("non_veg") is None and isinstance(cand.get("non_veg"), bool):
                    base["non_veg"] = cand.get("non_veg")
                if not base.get("dish_type"):
                    dish_type = str(cand.get("dish_type") or "").strip()
                    if dish_type:
                        base["dish_type"] = dish_type
                if not base.get("timings"):
                    timings = str(cand.get("timings") or "").strip()
                    if timings:
                        base["timings"] = timings
                if isinstance(cand.get("extra_attributes"), dict) and cand.get("extra_attributes"):
                    base["extra_attributes"] = dict(cand.get("extra_attributes") or {})
            merged_items.append(base)

        if not merged_items and cand_items:
            formatted["items"] = cand_items
            return formatted
        formatted["items"] = merged_items
        return formatted

    def _backfill_missing_item_fields_from_deterministic(self, formatted: Dict, deterministic: Dict) -> Dict:
        if not isinstance(formatted, dict):
            return deterministic if isinstance(deterministic, dict) else formatted
        out_items = formatted.get("items")
        det_items = deterministic.get("items") if isinstance(deterministic, dict) else None
        if not isinstance(out_items, list) or not isinstance(det_items, list) or not det_items:
            return formatted

        def norm_name(name: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())

        def _is_empty_value(val: Any) -> bool:
            if val is None:
                return True
            if isinstance(val, str):
                return len(val.strip()) == 0
            if isinstance(val, list):
                return len(val) == 0
            return False

        det_candidates = [d for d in det_items if isinstance(d, dict)]
        if not det_candidates:
            return formatted

        for item in out_items:
            if not isinstance(item, dict):
                continue
            need_price = _is_empty_value(item.get("price"))
            need_kcal = _is_empty_value(item.get("kcal"))
            need_desc = _is_empty_value(item.get("description"))
            need_dish_type = _is_empty_value(item.get("dish_type"))
            need_timings = _is_empty_value(item.get("timings"))
            if not (need_price or need_kcal or need_desc or need_dish_type or need_timings):
                continue

            name = str(item.get("name") or "")
            if not name.strip():
                continue
            key = norm_name(name)
            try:
                page_no = int(item.get("page"))
            except Exception:
                page_no = None

            best = None
            best_score = 0.0
            for det in det_candidates:
                dname = str(det.get("name") or "")
                dkey = norm_name(dname)
                if not dkey:
                    continue
                if key and dkey == key:
                    score = 1.0
                else:
                    score = SequenceMatcher(None, key, dkey).ratio() if key else 0.0
                if key and dkey and (key in dkey or dkey in key):
                    score = max(score, 0.88)
                if page_no is not None:
                    try:
                        dpage = int(det.get("page"))
                    except Exception:
                        dpage = None
                    if dpage == page_no:
                        score += 0.05
                if score > best_score:
                    best_score = score
                    best = det

            if not isinstance(best, dict) or best_score < 0.72:
                continue

            if need_price and not _is_empty_value(best.get("price")):
                item["price"] = best.get("price")
            if need_kcal and not _is_empty_value(best.get("kcal")):
                item["kcal"] = best.get("kcal")
            if need_desc:
                desc = str(best.get("description") or "").strip()
                if desc and not self._is_footer_text(desc):
                    item["description"] = desc
            if need_dish_type and not _is_empty_value(best.get("dish_type")):
                item["dish_type"] = best.get("dish_type")
            if need_timings and not _is_empty_value(best.get("timings")):
                item["timings"] = best.get("timings")

        formatted["items"] = out_items
        return formatted

    def _recover_missing_items_from_deterministic(self, formatted: Dict, deterministic: Dict) -> Tuple[Dict, bool]:
        if not isinstance(formatted, dict):
            return formatted, False
        out_items = formatted.get("items")
        det_items = deterministic.get("items") if isinstance(deterministic, dict) else None
        if not isinstance(out_items, list) or not isinstance(det_items, list) or not det_items:
            return formatted, False

        def _norm_name(text: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

        def _coerce_bool(val: Any) -> bool | None:
            if isinstance(val, bool):
                return val
            s = str(val or "").strip().lower()
            if s in {"true", "1", "yes"}:
                return True
            if s in {"false", "0", "no"}:
                return False
            return None

        fmt_dish_type_keys: set[str] = set()

        def _match_score(a: str, b: str) -> float:
            ak = _norm_name(a)
            bk = _norm_name(b)
            if not ak or not bk:
                return 0.0
            if ak == bk:
                return 1.0
            score = SequenceMatcher(None, ak, bk).ratio()
            if ak in bk or bk in ak:
                score = max(score, 0.9)
            return score

        def _clean_candidate_name(text: Any) -> str:
            name = self._clean_item_name(str(text or "")).strip()
            if not name:
                return ""
            name = re.sub(r"^\d+\)\s*", "", name)
            name = re.sub(r"^\([a-z0-9]{1,3}\)\s*", "", name, flags=re.IGNORECASE)
            name = re.sub(r"\(\s*\d+\s*\)\s*$", "", name)
            name = re.sub(r"\(\s*(?:v|vg|s|nv|nveg|veg|non_veg)\s*\)\s*$", "", name, flags=re.IGNORECASE)
            name = re.sub(r"\s+", " ", name).strip(" -_,.;:")
            toks = name.split()
            if toks and toks[-1].lower() in {"v", "vg", "s", "nv", "nveg", "veg", "non_veg"}:
                toks = toks[:-1]
            if len(toks) >= 2 and len(toks[-1]) == 1 and toks[-1].isalpha() and toks[-1].isupper():
                toks = toks[:-1]
            name = " ".join(toks).strip()
            return name

        def _is_kcal_only_name(name: str) -> bool:
            txt = re.sub(r"\s+", " ", str(name or "").strip().lower())
            if not txt:
                return False
            return (
                re.fullmatch(
                    r"[\W_]*\d{1,4}(?:[.,]\d{1,2})?\s*(kcal|cal|calorie|calories)\b[\W_]*",
                    txt,
                )
                is not None
            )

        def _is_heading_only(item_obj: Dict[str, Any], cleaned_name: str) -> bool:
            if not cleaned_name:
                return False
            price = str(item_obj.get("price") or "").strip()
            kcal = str(item_obj.get("kcal") or "").strip()
            desc = re.sub(r"\s+", " ", str(item_obj.get("description") or "")).strip()
            words = [w for w in re.findall(r"[A-Za-z]+", cleaned_name) if w]
            if len(words) > 4 or not words:
                return False
            heading_terms = {
                "starter",
                "starters",
                "appetizer",
                "appetizers",
                "small",
                "plates",
                "main",
                "mains",
                "soup",
                "soups",
                "salad",
                "salads",
                "beverage",
                "beverages",
                "drink",
                "drinks",
                "dessert",
                "desserts",
                "sandwich",
                "sandwiches",
                "burger",
                "burgers",
                "pizza",
                "pastas",
                "pasta",
                "rice",
                "noodles",
                "dimsum",
                "dumpling",
                "dumplings",
                "special",
                "specials",
                "chef",
                "recommended",
                "signature",
                "extras",
                "sides",
                "add",
                "ons",
                "dolci",
                "affogato",
                "aperitivo",
                "digestivo",
                "cocktail",
                "cocktails",
            }
            low_words = [w.lower() for w in words]
            name_key = _norm_name(cleaned_name)
            if not desc and name_key and name_key in fmt_dish_type_keys:
                return True
            if any(w in heading_terms for w in low_words):
                return True
            if price or kcal:
                if desc:
                    return False
                if len(words) >= 2:
                    return False
                alpha = [c for c in cleaned_name if c.isalpha()]
                if not alpha:
                    return False
                upper_ratio = sum(1 for c in alpha if c.isupper()) / max(len(alpha), 1)
                return upper_ratio >= 0.9
            if len(words) >= 2:
                return False
            alpha = [c for c in cleaned_name if c.isalpha()]
            if not alpha:
                return False
            upper_ratio = sum(1 for c in alpha if c.isupper()) / max(len(alpha), 1)
            return upper_ratio >= 0.85

        def _is_recovery_candidate(item_obj: Dict[str, Any]) -> Tuple[bool, str]:
            clean_name = _clean_candidate_name(item_obj.get("name"))
            if not clean_name:
                return False, clean_name
            has_price = bool(str(item_obj.get("price") or "").strip())
            if _is_kcal_only_name(clean_name):
                return False, clean_name
            if _is_heading_only(item_obj, clean_name):
                return False, clean_name
            if self._is_footer_text(clean_name.lower()):
                return False, clean_name
            if re.search(r"\s[-~]\s", clean_name):
                return False, clean_name
            if len(clean_name) > 56:
                return False, clean_name
            if sum(ch.isdigit() for ch in clean_name) / max(len(clean_name), 1) > 0.08:
                return False, clean_name
            words = [w for w in re.split(r"[^A-Za-z0-9&]+", clean_name) if w]
            if len(words) < 2 or len(words) > 7:
                return False, clean_name
            if not has_price:
                alpha = [c for c in clean_name if c.isalpha()]
                if not alpha:
                    return False, clean_name
                upper_ratio = sum(1 for c in alpha if c.isupper()) / max(len(alpha), 1)
                if upper_ratio < 0.6:
                    return False, clean_name
                desc_text = re.sub(r"\s+", " ", str(item_obj.get("description") or "")).strip()
                if desc_text:
                    if self._is_footer_text(desc_text):
                        return False, clean_name
                    if len(desc_text) > 260:
                        return False, clean_name
                    if len(re.findall(r"[A-Za-z]{3,}", desc_text)) < 1:
                        return False, clean_name
            if not self._looks_like_dish_line(clean_name):
                return False, clean_name
            return True, clean_name

        fmt_items = [it for it in out_items if isinstance(it, dict)]
        if not fmt_items:
            return formatted, False
        fmt_dish_type_keys = {
            _norm_name(it.get("dish_type") or "")
            for it in fmt_items
            if isinstance(it, dict) and _norm_name(it.get("dish_type") or "")
        }
        fmt_names = [str(it.get("name") or "").strip() for it in fmt_items]
        fmt_name_keys = [_norm_name(nm) for nm in fmt_names]

        present_map: Dict[int, int] = {}
        missing_candidates: List[Tuple[int, str, Dict[str, Any]]] = []

        for det_idx, det in enumerate(det_items):
            if not isinstance(det, dict):
                continue
            ok, clean_name = _is_recovery_candidate(det)
            if not ok:
                continue

            best_score = 0.0
            best_fmt_idx = -1
            for fi, fname in enumerate(fmt_names):
                score = _match_score(clean_name, fname)
                if score > best_score:
                    best_score = score
                    best_fmt_idx = fi

            if best_score >= 0.84 and best_fmt_idx >= 0:
                if det_idx not in present_map:
                    present_map[det_idx] = best_fmt_idx
                continue

            ckey = _norm_name(clean_name)
            if ckey:
                contained = False
                for fk in fmt_name_keys:
                    if not fk:
                        continue
                    if len(ckey) >= 5 and (ckey in fk or fk in ckey):
                        contained = True
                        break
                if contained:
                    continue

            missing_candidates.append((det_idx, clean_name, det))

        if not missing_candidates:
            return formatted, False

        max_add = max(6, int(len(fmt_items) * 0.6))
        if len(missing_candidates) > max_add:
            return formatted, False

        def _build_item(det_item: Dict[str, Any], clean_name: str) -> Dict[str, Any]:
            desc = str(det_item.get("description") or "").strip()
            if desc and self._is_footer_text(desc.lower()):
                desc = ""
            out = {
                "name": clean_name,
                "price": det_item.get("price"),
                "kcal": det_item.get("kcal"),
                "description": desc or None,
                "allergens": (
                    [str(a) for a in (det_item.get("allergens") or []) if str(a)]
                    if isinstance(det_item.get("allergens"), list)
                    else []
                ),
                "veg": _coerce_bool(det_item.get("veg")),
                "non_veg": _coerce_bool(det_item.get("non_veg")),
                "page": det_item.get("page") if det_item.get("page") is not None else 1,
                "dish_type": det_item.get("dish_type"),
                "timings": det_item.get("timings"),
            }
            if isinstance(det_item.get("extra_attributes"), dict) and det_item.get("extra_attributes"):
                out["extra_attributes"] = dict(det_item.get("extra_attributes"))
            return out

        merged = list(fmt_items)
        for det_idx, clean_name, det_item in sorted(missing_candidates, key=lambda x: x[0]):
            prev_keys = [k for k in present_map.keys() if k < det_idx]
            next_keys = [k for k in present_map.keys() if k > det_idx]
            if prev_keys:
                prev_key = max(prev_keys)
                insert_at = min(len(merged), present_map[prev_key] + 1)
            elif next_keys:
                next_key = min(next_keys)
                insert_at = max(0, present_map[next_key])
            else:
                insert_at = len(merged)

            merged.insert(insert_at, _build_item(det_item, clean_name))

            updated_map: Dict[int, int] = {}
            for k, v in present_map.items():
                updated_map[k] = (v + 1) if v >= insert_at else v
            updated_map[det_idx] = insert_at
            present_map = updated_map

        formatted["items"] = merged
        return formatted, True

    def _single_lowercase_name_token(self, text: Any) -> str | None:
        raw = self._clean_item_name(str(text or "")).strip()
        if not raw:
            return None
        raw = re.sub(r"\(([A-Za-z][A-Za-z0-9/&+\-]{0,7})\)", " ", raw)
        raw = re.sub(r"[\s~\-]*(?:â‚¹|rs\.?|inr|\$)?\s*\d{2,6}(?:[.,]\d{1,2})?\s*$", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s+", " ", raw).strip(" -_,.;:")
        if not raw:
            return None
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&\-]*", raw) if w]
        if len(words) != 1:
            return None
        token = words[0].strip("-'").lower()
        if len(token) < 5 or len(token) > 24:
            return None
        if token != words[0].strip("-'"):
            return None
        return token

    def _drop_likely_fragment_items(self, formatted: Dict) -> Tuple[Dict, bool]:
        if not isinstance(formatted, dict):
            return formatted, False
        items = formatted.get("items")
        if not isinstance(items, list) or not items:
            return formatted, False

        def _name_word_count(name: Any) -> int:
            return len([w for w in re.findall(r"[A-Za-z][A-Za-z'&\-]*", str(name or "")) if w])

        def _same_page(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
            try:
                pa = int(a.get("page"))
                pb = int(b.get("page"))
                return pa == pb
            except Exception:
                return True

        def _nearest_neighbor(idx: int, step: int) -> Dict[str, Any] | None:
            j = idx + step
            while 0 <= j < len(items):
                cand = items[j]
                if isinstance(cand, dict):
                    return cand
                j += step
            return None

        def _window_neighbors(idx: int, radius: int = 4) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            lo = max(0, idx - radius)
            hi = min(len(items), idx + radius + 1)
            for j in range(lo, hi):
                if j == idx:
                    continue
                cand = items[j]
                if isinstance(cand, dict):
                    out.append(cand)
            return out

        def _single_word_token(name: Any) -> str | None:
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&\-]*", str(name or "")) if w]
            if len(words) != 1:
                return None
            return words[0].strip("-'").lower() or None

        def _trim_footer_noise(description: Any) -> Tuple[str, bool]:
            desc = re.sub(r"\s+", " ", str(description or "")).strip()
            if not desc:
                return "", False
            low = desc.lower()
            cut_markers = [
                "please let us",
                "before you order",
                "some dishes can be made",
                "gluten free",
                "taxes",
                "charged extra",
                "exclusive of taxes",
            ]
            cut_at = None
            for marker in cut_markers:
                pos = low.find(marker)
                if pos >= 0:
                    cut_at = pos if cut_at is None else min(cut_at, pos)
            if cut_at is None:
                trimmed = desc
                changed = False
            else:
                trimmed = re.sub(r"\s+", " ", desc[:cut_at]).strip(" -_,.;:")
                changed = True

            # Remove OCR tail noise fragments like "ow o RE you Some a o"
            # while preserving normal ingredient descriptions.
            tokens = re.findall(r"[A-Za-z]+", trimmed)
            if len(tokens) >= 6:
                tail_tokens = tokens[-6:]
                short_count = sum(1 for t in tail_tokens if len(t) <= 3)
                alpha_short_ratio = short_count / max(len(tail_tokens), 1)
                if alpha_short_ratio >= 0.75:
                    # Drop final weak tail by trimming from the first of the noisy tail tokens.
                    marker = tail_tokens[0]
                    m = re.search(rf"\b{re.escape(marker)}\b", trimmed, flags=re.IGNORECASE)
                    if m and m.start() > 0:
                        trimmed = re.sub(r"\s+", " ", trimmed[: m.start()]).strip(" -_,.;:")
                        changed = True
            # Final cleanup: strip very short orphan tail tokens.
            m_tail = re.search(r"(?:\s+[A-Za-z]{1,2}){1,3}\s*$", trimmed)
            if m_tail and m_tail.start() > 0:
                base = re.sub(r"\s+", " ", trimmed[: m_tail.start()]).strip(" -_,.;:")
                if len(base) >= 8:
                    trimmed = base
                    changed = True
            return trimmed, changed

        structural_line_re = re.compile(r"^\[?\s*(?:column|page)\s*\d+\s*\]?$", flags=re.IGNORECASE)
        menu_name = str(formatted.get("menu_name") or "").strip().lower()
        menu_tokens = [t for t in re.findall(r"[a-z]{3,}", menu_name) if t]
        generic_menu_tokens = {
            "menu",
            "dessert",
            "desserts",
            "beverage",
            "beverages",
            "breakfast",
            "lunch",
            "dinner",
            "food",
            "drinks",
            "drink",
            "room",
            "dining",
            "in",
        }
        brand_tokens = [t for t in menu_tokens if t not in generic_menu_tokens]
        primary_brand = brand_tokens[0] if brand_tokens else None
        noise_single_tokens = {
            "some",
            "please",
            "taxes",
            "order",
            "allergies",
            "allergy",
            "gluten",
            "free",
            "home",
            "download",
        }

        dropped_names: List[str] = []
        kept: List[Dict[str, Any]] = []
        changed = False
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            working = dict(item)
            desc_val = working.get("description")
            trimmed_desc, desc_trimmed = _trim_footer_noise(desc_val)
            if desc_trimmed:
                working["description"] = trimmed_desc if trimmed_desc else None
                changed = True

            name_raw = str(working.get("name") or "").strip()
            low_name = re.sub(r"\s+", " ", name_raw).strip().lower()
            if structural_line_re.fullmatch(low_name) or low_name in {"[body]", "[footer]", "[shared]", "column", "page"}:
                if name_raw:
                    dropped_names.append(name_raw)
                changed = True
                continue
            if self._looks_like_icon_noise_line(name_raw):
                if name_raw:
                    dropped_names.append(name_raw)
                changed = True
                continue

            if (
                "+" in name_raw
                and not str(working.get("description") or "").strip()
                and not self._normalize_price(str(working.get("price") or ""))
                and not self._normalize_kcal(str(working.get("kcal") or ""))
            ):
                dropped_names.append(name_raw)
                changed = True
                continue

            one_word = _single_word_token(name_raw)
            if one_word and one_word in noise_single_tokens:
                if self._is_footer_text(str(working.get("description") or "")) or not str(working.get("description") or "").strip():
                    if name_raw:
                        dropped_names.append(name_raw)
                    changed = True
                    continue

            if (
                one_word
                and primary_brand
                and one_word == primary_brand
                and not str(working.get("description") or "").strip()
                and not self._normalize_kcal(str(working.get("kcal") or ""))
                and not isinstance(working.get("extra_attributes"), dict)
                and working.get("veg") is not True
                and working.get("non_veg") is not True
            ):
                dropped_names.append(name_raw)
                changed = True
                continue

            token = self._single_lowercase_name_token(working.get("name"))
            if not token:
                kept.append(working)
                continue
            if str(working.get("description") or "").strip():
                kept.append(working)
                continue
            if self._normalize_kcal(str(working.get("kcal") or "")):
                kept.append(working)
                continue
            extra = working.get("extra_attributes")
            if isinstance(extra, dict) and extra:
                kept.append(working)
                continue
            if working.get("veg") is True or working.get("non_veg") is True:
                kept.append(working)
                continue

            price = self._normalize_price(str(working.get("price") or "")) or ""
            prev_item = _nearest_neighbor(idx, -1)
            next_item = _nearest_neighbor(idx, +1)
            neighbors = [n for n in (prev_item, next_item) if isinstance(n, dict) and _same_page(working, n)]
            wider_neighbors = [n for n in _window_neighbors(idx, radius=4) if _same_page(working, n)]

            same_price_neighbor = False
            if price:
                for n in wider_neighbors:
                    n_price = self._normalize_price(str(n.get("price") or "")) or ""
                    if n_price != price:
                        continue
                    if _name_word_count(n.get("name")) >= 2:
                        same_price_neighbor = True
                        break

            token_in_neighbor_desc = False
            for n in wider_neighbors:
                desc = str(n.get("description") or "").lower()
                if token and token in desc:
                    token_in_neighbor_desc = True
                    break

            if same_price_neighbor or token_in_neighbor_desc:
                nm = str(working.get("name") or "").strip()
                if nm:
                    dropped_names.append(nm)
                changed = True
                continue
            kept.append(working)

        def _norm_key(v: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(v or "").lower())

        # Dedupe section-prefixed variants:
        # keep "AFFOGATO SMORE" and drop ambiguous "SMORE" when both appear.
        suffix_to_prefixed: Dict[str, List[Dict[str, Any]]] = {}
        for it in kept:
            if not isinstance(it, dict):
                continue
            nkey = _norm_key(it.get("name"))
            dkey = _norm_key(it.get("dish_type"))
            if not nkey or not dkey:
                continue
            if nkey.startswith(dkey) and len(nkey) > (len(dkey) + 3):
                suffix = nkey[len(dkey) :]
                if len(suffix) >= 4:
                    suffix_to_prefixed.setdefault(suffix, []).append(it)

        if suffix_to_prefixed:
            generic_dish_types = {
                "",
                "dessert",
                "desserts",
                "beverage",
                "beverages",
                "drink",
                "drinks",
                "menu",
                "food",
            }
            deduped: List[Dict[str, Any]] = []
            for it in kept:
                if not isinstance(it, dict):
                    continue
                nkey = _norm_key(it.get("name"))
                dkey = _norm_key(it.get("dish_type"))
                if nkey in suffix_to_prefixed and dkey in generic_dish_types:
                    nm = str(it.get("name") or "").strip()
                    if nm:
                        dropped_names.append(nm)
                    changed = True
                    continue
                deduped.append(it)
            kept = deduped

        if len(kept) == len(items) and not changed:
            return formatted, False

        out = dict(formatted)
        out["items"] = kept
        other = out.get("other_text")
        other_list = list(other) if isinstance(other, list) else []
        for nm in dropped_names:
            if nm and nm not in other_list:
                other_list.append(nm)
        out["other_text"] = other_list
        return out, True

    def _strip_ui_wrappers(self, text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        return cleaned

    def _compact_openai_raw_text(self, text: str) -> str:
        cleaned = self._strip_ui_wrappers(text)
        if not cleaned:
            return cleaned
        compact_lines: List[str] = []
        for raw_line in cleaned.splitlines():
            line = re.sub(r"\s+", " ", str(raw_line or "")).strip()
            if not line:
                continue
            lower = line.lower()
            if lower.startswith("[page") or lower.startswith("[column") or lower in {"[shared]", "[body]", "[footer]"}:
                compact_lines.append(line)
                continue
            # Drop frequent legend/footer noise to keep token count lower during retries.
            if "signature dish" in lower and "celery" in lower and "lupin" in lower:
                continue
            if lower.startswith("ct:") and "crustacean" in lower:
                continue
            compact_lines.append(line)
        return "\n".join(compact_lines).strip()

    def _split_raw_text_page_blocks(self, raw_text: str) -> List[Tuple[int, str]]:
        cleaned = self._strip_ui_wrappers(raw_text)
        if not cleaned:
            return []
        lines = cleaned.splitlines()
        blocks: List[Tuple[int, List[str]]] = []
        current_page = None
        current_lines: List[str] = []
        for raw_line in lines:
            line = re.sub(r"\s+", " ", str(raw_line or "")).strip()
            if not line:
                continue
            m = re.match(r"^\[PAGE\s+(\d+)\]", line, flags=re.IGNORECASE)
            if m:
                if current_lines:
                    blocks.append((current_page or 1, current_lines))
                current_page = int(m.group(1))
                current_lines = [line]
            else:
                current_lines.append(line)
        if current_lines:
            blocks.append((current_page or 1, current_lines))
        return [(pg, "\n".join(bl).strip()) for pg, bl in blocks if bl]

    def _build_openai_raw_text_chunks(self, raw_text: str, max_pages_per_chunk: int = 5, max_chars: int = 7000) -> List[Dict[str, Any]]:
        blocks = self._split_raw_text_page_blocks(raw_text)
        if not blocks:
            return []
        chunks: List[Dict[str, Any]] = []
        cur_pages: List[int] = []
        cur_texts: List[str] = []
        cur_chars = 0
        for page_no, block_text in blocks:
            block_len = len(block_text)
            need_flush = False
            if cur_texts and len(cur_pages) >= max_pages_per_chunk:
                need_flush = True
            if cur_texts and (cur_chars + block_len) > max_chars:
                need_flush = True
            if need_flush:
                chunks.append({"pages": cur_pages[:], "raw_text": "\n".join(cur_texts).strip()})
                cur_pages = []
                cur_texts = []
                cur_chars = 0
            cur_pages.append(page_no)
            cur_texts.append(block_text)
            cur_chars += block_len + 1
        if cur_texts:
            chunks.append({"pages": cur_pages[:], "raw_text": "\n".join(cur_texts).strip()})
        return [c for c in chunks if c.get("raw_text")]

    def _as_text_list(self, val: Any) -> List[str]:
        if val is None:
            return []
        if isinstance(val, list):
            out: List[str] = []
            for x in val:
                s = str(x or "").strip()
                if s:
                    out.append(s)
            return out
        s = str(val).strip()
        return [s] if s else []

    def _merge_chunked_openai_parsed(self, parsed_chunks: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        if not parsed_chunks:
            return None
        menu_name = None
        items: List[Dict[str, Any]] = []
        other_text: List[str] = []
        footer_text: List[str] = []
        notes: List[str] = []
        seen_item_keys = set()

        def add_text_unique(target: List[str], values: List[str]) -> None:
            for s in values:
                if s not in target:
                    target.append(s)

        for chunk in parsed_chunks:
            if not isinstance(chunk, dict):
                continue
            if not menu_name:
                mn = str(chunk.get("menu_name") or "").strip()
                if mn:
                    menu_name = mn
            add_text_unique(other_text, self._as_text_list(chunk.get("other_text")))
            add_text_unique(footer_text, self._as_text_list(chunk.get("footer_text")))
            add_text_unique(notes, self._as_text_list(chunk.get("notes")))
            chunk_items = chunk.get("items")
            if not isinstance(chunk_items, list):
                continue
            for item in chunk_items:
                if not isinstance(item, dict):
                    continue
                key = (
                    re.sub(r"[^a-z0-9]+", "", str(item.get("name") or "").lower()),
                    str(item.get("page") or ""),
                    re.sub(r"[^a-z0-9]+", "", str(item.get("dish_type") or "").lower()),
                    str(item.get("price") or ""),
                )
                if key in seen_item_keys:
                    continue
                seen_item_keys.add(key)
                items.append(item)

        if not items:
            return None
        return {
            "menu_name": menu_name,
            "items": items,
            "other_text": other_text,
            "footer_text": footer_text,
            "notes": notes,
        }

    def _run_chunked_openai_fallback(
        self,
        raw_text: str,
        row_column_details: Dict[str, Any] | None,
        layout_lines: Dict[str, Any] | None,
        icon_lines: List[Dict[str, Any]] | None,
        icon_detections: List[Dict[str, Any]] | None,
        call_openai: Any,
        base_timeout: int,
        max_pages_per_chunk: int = 5,
    ) -> Dict[str, Any] | None:
        chunks = self._build_openai_raw_text_chunks(raw_text, max_pages_per_chunk=max_pages_per_chunk)
        if not chunks:
            return None

        def _subset_page_object(obj: Dict[str, Any] | None, pages: List[int]) -> Dict[str, Any] | None:
            if not isinstance(obj, dict):
                return None
            src_pages = obj.get("pages")
            if not isinstance(src_pages, list) or not src_pages:
                return None
            page_set = set(pages)
            out_pages: List[Dict[str, Any]] = []
            for entry in src_pages:
                if not isinstance(entry, dict):
                    continue
                try:
                    pg = int(entry.get("page"))
                except Exception:
                    continue
                if pg in page_set:
                    out_pages.append(entry)
            if not out_pages:
                return None
            return {"pages": out_pages}

        def _subset_page_list(entries: List[Dict[str, Any]] | None, pages: List[int]) -> List[Dict[str, Any]] | None:
            if not isinstance(entries, list) or not entries:
                return None
            page_set = set(pages)
            out: List[Dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    pg = int(entry.get("page"))
                except Exception:
                    continue
                if pg in page_set:
                    out.append(entry)
            return out or None

        parsed_chunks: List[Dict[str, Any]] = []
        chunk_count = len(chunks)
        for idx, chunk in enumerate(chunks):
            pages = [int(x) for x in (chunk.get("pages") or []) if isinstance(x, int) or str(x).isdigit()]
            chunk_user: Dict[str, Any] = {
                "source": "google_document_ai_raw_text_chunk",
                "instruction": (
                    "This is OCR text from specific pages. Parse it page by page in reading order, "
                    "using coordinates and layout metadata where provided. "
                    "Return valid menu JSON for this chunk. "
                    "Do not drop any non-empty line; map non-item lines to other_text/footer_text/notes."
                ),
                "raw_text": chunk.get("raw_text"),
                "chunk_index": idx + 1,
                "chunk_total": chunk_count,
            }
            if pages:
                chunk_user["chunk_page_range"] = {"start": min(pages), "end": max(pages)}
            if len(pages) == 1:
                chunk_user["chunk_mode"] = "single_page"

            subset_row_col = _subset_page_object(row_column_details, pages)
            if subset_row_col:
                chunk_user["row_column_details"] = subset_row_col

            subset_layout = _subset_page_object(layout_lines, pages)
            if subset_layout:
                chunk_user["layout_lines"] = subset_layout

            subset_icon_lines = _subset_page_list(icon_lines, pages)
            if subset_icon_lines:
                chunk_user["icon_lines"] = subset_icon_lines

            subset_icon_dets = _subset_page_list(icon_detections, pages)
            if subset_icon_dets:
                chunk_user["icon_detections"] = subset_icon_dets

            chunk_response = None
            for timeout_sec in (max(base_timeout, 120), max(base_timeout, 180), max(base_timeout, 240)):
                try:
                    resp = call_openai(chunk_user, timeout_sec)
                    raw = resp.output[0].content[0].text
                    parsed = self._parse_json_maybe(raw)
                    if isinstance(parsed, dict):
                        chunk_response = parsed
                        break
                except Exception:
                    continue
            if not isinstance(chunk_response, dict):
                return None
            parsed_chunks.append(chunk_response)

        return self._merge_chunked_openai_parsed(parsed_chunks)

    def _shared_price_value(self, line: str) -> str | None:
        text = re.sub(r"\s+", " ", str(line or "")).strip()
        if not text:
            return None
        m = re.match(r"^~\s*(\d{2,6}(?:[.,]\d{1,2})?)\s*$", text)
        if not m:
            return None
        return m.group(1).replace(",", ".")

    def _is_shared_price_section_heading(self, line: str) -> bool:
        text = self._clean_heading_text(line)
        if not text:
            return False
        if self._shared_price_value(text):
            return False
        lower = text.lower()
        if lower.startswith("[page") or lower.startswith("[column") or lower in {"[body]", "[footer]", "[shared]"}:
            return False
        if re.fullmatch(r"\[?\s*(?:column|page)\s*\d+\s*\]?", lower):
            return False
        if text in {"+", "-", "|", "/", "~"}:
            return False
        if self._looks_like_icon_noise_line(text):
            return False
        if any(lower.startswith(pfx) for pfx in ("please let us know", "some dishes can be made", "taxes are")):
            return False
        if self._is_footer_text(lower):
            return False
        if len(text) > 48:
            return False
        if any(ch.isdigit() for ch in text):
            return False
        letters = [c for c in text if c.isalpha()]
        if len(letters) < 3:
            return False
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio >= 0.62:
            return True
        if "+" in text and len(text.split()) <= 5:
            return True
        return False

    def _is_shared_price_item_name(self, line: str) -> bool:
        text = self._clean_heading_text(line)
        if not text:
            return False
        if self._shared_price_value(text):
            return False
        lower = text.lower()
        if lower.startswith("[page") or lower.startswith("[column") or lower in {"[body]", "[footer]", "[shared]"}:
            return False
        if re.fullmatch(r"\[?\s*(?:column|page)\s*\d+\s*\]?", lower):
            return False
        if text in {"+", "-", "|", "/", "~"}:
            return False
        if self._looks_like_icon_noise_line(text):
            return False
        if any(lower.startswith(pfx) for pfx in ("please let us know", "some dishes can be made", "taxes are")):
            return False
        if self._is_footer_text(lower):
            return False
        if len(text) > 64:
            return False
        if sum(ch.isdigit() for ch in text) / max(len(text), 1) > 0.15:
            return False
        letters = [c for c in text if c.isalpha()]
        if len(letters) < 2:
            return False
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        words = [w for w in re.split(r"[^A-Za-z0-9]+", text) if w]
        if not words:
            return False
        if "," in text and upper_ratio < 0.82:
            return False
        if len(words) > 7 and upper_ratio < 0.82:
            return False
        if upper_ratio >= 0.65:
            return True
        title_ratio = sum(1 for w in words if w[:1].isalpha() and w[:1].isupper()) / len(words)
        if title_ratio >= 0.9 and len(words) <= 4:
            return True
        return False

    def _should_prefix_section_name(self, heading: str, section_items: List[Dict[str, Any]]) -> bool:
        head = self._clean_heading_text(heading)
        if not head:
            return False
        lower = head.lower()
        if any(ch in head for ch in ("+", "/", "&")):
            return False
        words = [w for w in re.split(r"[^A-Za-z0-9]+", head) if w]
        if len(words) != 1:
            return False
        generic_sections = {
            "dessert",
            "desserts",
            "dolci",
            "aperitivo",
            "digestivo",
            "drinks",
            "beverage",
            "beverages",
            "breakfast",
            "lunch",
            "dinner",
            "starter",
            "starters",
            "main",
            "mains",
            "pasta",
            "pizza",
            "soup",
            "soups",
            "salad",
            "salads",
            "sandwich",
            "sandwiches",
            "burger",
            "burgers",
            "buns",
        }
        if any(tok in generic_sections for tok in re.split(r"[^a-z]+", lower) if tok):
            return False
        if len(section_items) < 2:
            return False
        short_name_count = 0
        for it in section_items:
            if not isinstance(it, dict):
                continue
            nm = self._clean_item_name(it.get("name") or "")
            if not nm:
                continue
            if self._clean_heading_text(nm).lower().startswith(lower):
                return False
            nwords = [w for w in re.split(r"[^A-Za-z0-9]+", nm) if w]
            if 1 <= len(nwords) <= 4:
                short_name_count += 1
        needed = max(2, int(len(section_items) * 0.7))
        return short_name_count >= needed

    def _build_shared_price_section_items(self, raw_text: str, page_no: int | None = 1) -> Dict[str, Any] | None:
        cleaned = self._strip_ui_wrappers(raw_text)
        if not cleaned:
            return None

        # Parse column blocks independently so shared-price propagation does not
        # bleed across unrelated columns/sections.
        column_blocks: List[List[str]] = []
        current_block: List[str] = []
        for raw_line in cleaned.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            lower = line.lower()
            if lower.startswith("[column"):
                if current_block:
                    column_blocks.append(current_block)
                current_block = []
                continue
            if lower.startswith("[page") or lower in {"[body]", "[footer]", "[shared]"}:
                continue
            if any(lower.startswith(pfx) for pfx in ("please let us know", "some dishes can be made", "taxes are")):
                continue
            if self._is_footer_text(lower):
                continue
            if self._looks_like_icon_noise_line(line):
                continue
            current_block.append(line)
        if current_block:
            column_blocks.append(current_block)
        if not column_blocks:
            return None

        def _extract_sections(lines: List[str]) -> List[Dict[str, Any]]:
            out_sections: List[Dict[str, Any]] = []
            if len(lines) < 5:
                return out_sections
            current_section: Dict[str, Any] | None = None
            current_item: Dict[str, Any] | None = None
            idx = 0
            while idx < len(lines):
                line = lines[idx]
                next_price = self._shared_price_value(lines[idx + 1]) if (idx + 1) < len(lines) else None
                if next_price and self._is_shared_price_section_heading(line):
                    if current_item is not None and current_section is not None:
                        current_section["items"].append(current_item)
                        current_item = None
                    if current_section is not None and current_section.get("items"):
                        out_sections.append(current_section)
                    current_section = {
                        "heading": self._clean_heading_text(line),
                        "price": next_price,
                        "items": [],
                    }
                    idx += 2
                    continue

                if current_section is None:
                    idx += 1
                    continue

                if self._is_shared_price_item_name(line):
                    if current_item is not None:
                        current_section["items"].append(current_item)
                    current_item = {"name": self._clean_item_name(line), "description_lines": []}
                elif current_item is not None:
                    if not self._is_footer_text(line) and not self._looks_like_icon_noise_line(line):
                        current_item["description_lines"].append(line)
                idx += 1

            if current_item is not None and current_section is not None:
                current_section["items"].append(current_item)
            if current_section is not None and current_section.get("items"):
                out_sections.append(current_section)
            return out_sections

        sections: List[Dict[str, Any]] = []
        for block in column_blocks:
            sections.extend(_extract_sections(block))

        if len(sections) < 2:
            return None
        if any(not s.get("items") for s in sections):
            return None

        items: List[Dict[str, Any]] = []
        prefix_pairs: List[Tuple[str, str]] = []
        section_names: List[str] = []
        for section in sections:
            heading = self._clean_heading_text(section.get("heading") or "")
            price = section.get("price")
            if not heading or not price:
                continue
            section_names.append(heading)
            prefix_name = self._should_prefix_section_name(heading, section.get("items") or [])
            for it in section.get("items", []):
                name = self._clean_item_name(it.get("name") or "")
                if not name:
                    continue
                if prefix_name:
                    base_name = name
                    name = f"{heading} {name}".strip()
                    prefix_pairs.append((base_name, name))
                desc_lines = [re.sub(r"\s+", " ", str(x or "")).strip() for x in (it.get("description_lines") or [])]
                desc_lines = [x for x in desc_lines if x and not self._is_footer_text(x)]
                items.append(
                    {
                        "name": name,
                        "price": price,
                        "kcal": None,
                        "description": " ".join(desc_lines).strip(),
                        "allergens": None,
                        "veg": None,
                        "non_veg": None,
                        "page": page_no,
                        "dish_type": heading,
                        "timings": None,
                    }
                )

        if len(items) < 6:
            return None
        return {"section_names": section_names, "items": items, "prefix_pairs": prefix_pairs}

    def _maybe_fix_shared_price_section_output(self, raw_text: str, parsed: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        parsed_items = parsed.get("items")
        if not isinstance(parsed_items, list) or len(parsed_items) < 4:
            return parsed, False

        page_no = None
        for item in parsed_items:
            if isinstance(item, dict) and item.get("page") is not None:
                page_no = item.get("page")
                break
        if page_no is None:
            page_no = 1

        candidate = self._build_shared_price_section_items(raw_text, page_no=page_no)
        if not candidate:
            return parsed, False

        def norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

        parsed_name_set = set()
        for item in parsed_items:
            if not isinstance(item, dict):
                continue
            n = norm(item.get("name") or "")
            if n:
                parsed_name_set.add(n)
        if not parsed_name_set:
            return parsed, False

        section_name_set = {norm(s) for s in candidate.get("section_names", []) if norm(s)}
        if not section_name_set:
            return parsed, False

        heading_as_item_count = len(parsed_name_set & section_name_set)

        prefix_pairs = candidate.get("prefix_pairs") if isinstance(candidate, dict) else None
        prefix_needed = False
        unprefixed_candidate_names = set()
        if isinstance(prefix_pairs, list):
            for pair in prefix_pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                base_norm = norm(pair[0])
                pref_norm = norm(pair[1])
                if base_norm:
                    unprefixed_candidate_names.add(base_norm)
                if base_norm and base_norm in parsed_name_set and pref_norm and pref_norm not in parsed_name_set:
                    prefix_needed = True

        if heading_as_item_count < 1 and not prefix_needed:
            return parsed, False

        candidate_items = candidate.get("items") or []
        candidate_name_set = {norm(it.get("name") or "") for it in candidate_items if isinstance(it, dict)}
        candidate_name_set.discard("")
        compare_name_set = set(candidate_name_set) | set(unprefixed_candidate_names)
        if not compare_name_set:
            return parsed, False

        overlap = len((parsed_name_set - section_name_set) & compare_name_set)
        if overlap < max(2, int(len(compare_name_set) * 0.25)):
            return parsed, False

        # Merge fixed shared-price items back into parsed output instead of replacing
        # everything, so unrelated columns/sections stay intact.
        candidate_by_name: Dict[str, Dict[str, Any]] = {}
        candidate_suffix_to_full: Dict[str, set[str]] = {}
        for it in candidate_items:
            if not isinstance(it, dict):
                continue
            nk = norm(it.get("name") or "")
            if nk and nk not in candidate_by_name:
                candidate_by_name[nk] = it
            dk = norm(it.get("dish_type") or "")
            if dk and nk.startswith(dk) and len(nk) > (len(dk) + 2):
                suffix = nk[len(dk) :]
                if len(suffix) >= 4:
                    candidate_suffix_to_full.setdefault(suffix, set()).add(nk)

        merged_items: List[Dict[str, Any]] = []
        seen_names: set[str] = set()
        used_candidate_names: set[str] = set()

        for item in parsed_items:
            if not isinstance(item, dict):
                continue
            nm_raw = str(item.get("name") or "").strip()
            nkey = norm(nm_raw)
            if nkey in section_name_set:
                continue
            if re.fullmatch(r"\[?\s*(?:column|page)\s*\d+\s*\]?", nm_raw.lower()):
                continue
            if nkey and nkey not in candidate_by_name and nkey in candidate_suffix_to_full:
                parsed_dish_type = norm(item.get("dish_type") or "")
                if not parsed_dish_type or parsed_dish_type in section_name_set:
                    continue
            if nkey and nkey in candidate_by_name:
                merged = dict(candidate_by_name[nkey])
                merged_items.append(merged)
                used_candidate_names.add(nkey)
                seen_names.add(nkey)
                continue
            merged_items.append(item)
            if nkey:
                seen_names.add(nkey)

        for it in candidate_items:
            if not isinstance(it, dict):
                continue
            nkey = norm(it.get("name") or "")
            if not nkey:
                continue
            if nkey in used_candidate_names or nkey in seen_names:
                continue
            merged_items.append(it)
            seen_names.add(nkey)

        if len(merged_items) < max(4, int(len(parsed_items) * 0.5)):
            return parsed, False

        fixed = dict(parsed)
        fixed["items"] = merged_items
        return fixed, True

    def _interleaved_section_price(self, line: str) -> str | None:
        text = re.sub(r"\s+", " ", str(line or "")).strip()
        if not text:
            return None
        m = re.fullmatch(r"(?:\$|â‚¹|rs\.?\s*|inr\s*)?\s*(\d{1,4}(?:[.,]\d{1,2})?)", text, flags=re.IGNORECASE)
        if not m:
            return None
        val = m.group(1).replace(",", ".")
        if "." not in val and len(val) <= 2:
            return None
        return f"${val}" if "$" in text else val

    def _split_interleaved_columns(self, raw_text: str) -> Dict[str, List[str]]:
        cleaned = self._strip_ui_wrappers(raw_text)
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in cleaned.splitlines()]
        lines = [ln for ln in lines if ln]
        cols: Dict[str, List[str]] = {"column_1": [], "column_2": []}
        current_col = None
        for line in lines:
            lower = line.lower()
            if lower == "[column 1]":
                current_col = "column_1"
                continue
            if lower == "[column 2]":
                current_col = "column_2"
                continue
            if lower.startswith("[page") or lower in {"[shared]", "[body]", "[footer]"}:
                continue
            if current_col in cols:
                cols[current_col].append(line)
        return cols

    def _split_joined_item_pair(self, text: str) -> List[str]:
        line = re.sub(r"\s+", " ", str(text or "")).strip()
        if not line:
            return []
        boundaries = list(re.finditer(r"(?<=[a-z])(?=[A-Z])", line))
        if len(boundaries) != 1:
            return [line]
        cut = boundaries[0].start()
        left = line[:cut].strip(" -â€“â€”,.;:")
        right = line[cut:].strip(" -â€“â€”,.;:")
        if len(left.split()) >= 2 and len(right.split()) >= 2:
            return [left, right]
        return [line]

    def _build_interleaved_beverage_dessert_items(self, raw_text: str, page_no: int | None = 1) -> Dict[str, Any] | None:
        cols = self._split_interleaved_columns(raw_text)
        col1 = cols.get("column_1") or []
        col2 = cols.get("column_2") or []
        if not col1 or not col2:
            return None

        def idx_of_heading(lines: List[str], aliases: set[str]) -> int | None:
            for idx, line in enumerate(lines):
                norm = re.sub(r"[^a-z]+", "", line.lower())
                if norm in aliases:
                    return idx
            return None

        bev_idx = idx_of_heading(col1, {"beverage", "beverages"})
        des_idx = idx_of_heading(col2, {"dessert", "desserts"})
        if bev_idx is None or des_idx is None:
            return None

        bev_heading = self._clean_heading_text(col1[bev_idx]) or "Beverage"
        des_heading = self._clean_heading_text(col2[des_idx]) or "Dessert"
        beverage_lines_raw = col1[bev_idx + 1 :]
        dessert_tokens = col2[des_idx + 1 :]
        if not beverage_lines_raw or not dessert_tokens:
            return None

        beverage_names: List[str] = []
        for line in beverage_lines_raw:
            if self._interleaved_section_price(line):
                continue
            if not re.search(r"[A-Za-z]", line):
                continue
            if self._is_footer_text(line):
                continue
            beverage_names.append(self._clean_item_name(line))
        beverage_names = [x for x in beverage_names if x]
        if len(beverage_names) < 3:
            return None

        expanded_names: List[str] = []
        for name in beverage_names:
            expanded_names.extend(self._split_joined_item_pair(name))
        if len(expanded_names) > len(beverage_names):
            beverage_names = expanded_names

        beverage_prices: List[str] = []
        dessert_pairs: List[Tuple[str, str]] = []
        pending_prices: List[str] = []
        idx = 0
        while idx < len(dessert_tokens):
            token = dessert_tokens[idx]
            price = self._interleaved_section_price(token)
            if price:
                pending_prices.append(price)
                idx += 1
                continue
            if not re.search(r"[A-Za-z]", token):
                idx += 1
                continue
            name = self._clean_item_name(token)
            if not name:
                idx += 1
                continue
            beverage_price = pending_prices.pop(0) if pending_prices else None
            dessert_price = pending_prices.pop(0) if pending_prices else None
            if dessert_price is None and idx + 1 < len(dessert_tokens):
                next_price = self._interleaved_section_price(dessert_tokens[idx + 1])
                if next_price:
                    dessert_price = next_price
                    idx += 1
            if beverage_price and dessert_price:
                beverage_prices.append(beverage_price)
                dessert_pairs.append((name, dessert_price))
            idx += 1

        if len(dessert_pairs) < 4 or len(beverage_prices) < 4:
            return None
        if len(beverage_names) < len(beverage_prices):
            expanded_once: List[str] = []
            for name in beverage_names:
                expanded_once.extend(self._split_joined_item_pair(name))
            if len(expanded_once) >= len(beverage_prices):
                beverage_names = expanded_once
        if len(beverage_names) != len(beverage_prices):
            return None

        items: List[Dict[str, Any]] = []
        for name, price in zip(beverage_names, beverage_prices):
            items.append(
                {
                    "name": name,
                    "price": price,
                    "kcal": None,
                    "description": None,
                    "allergens": None,
                    "veg": None,
                    "non_veg": None,
                    "page": page_no,
                    "dish_type": bev_heading,
                    "timings": None,
                }
            )
        for name, price in dessert_pairs:
            items.append(
                {
                    "name": name,
                    "price": price,
                    "kcal": None,
                    "description": None,
                    "allergens": None,
                    "veg": None,
                    "non_veg": None,
                    "page": page_no,
                    "dish_type": des_heading,
                    "timings": None,
                }
            )

        if len(items) < 8:
            return None
        return {
            "items": items,
            "beverage_names": beverage_names,
            "dessert_names": [nm for nm, _ in dessert_pairs],
            "beverage_heading": bev_heading,
            "dessert_heading": des_heading,
        }

    def _maybe_fix_interleaved_beverage_dessert_output(self, raw_text: str, parsed: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        parsed_items = parsed.get("items")
        if not isinstance(parsed_items, list) or len(parsed_items) < 6:
            return parsed, False

        def norm(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

        def dish_norm(item: Dict[str, Any]) -> str:
            return norm(item.get("dish_type") or "")

        beverage_items = [it for it in parsed_items if isinstance(it, dict) and dish_norm(it) in {"beverage", "beverages"}]
        dessert_items = [it for it in parsed_items if isinstance(it, dict) and dish_norm(it) in {"dessert", "desserts"}]
        if len(beverage_items) >= 2 or len(dessert_items) < 4:
            return parsed, False

        page_no = None
        for item in parsed_items:
            if isinstance(item, dict) and item.get("page") is not None:
                page_no = item.get("page")
                break
        if page_no is None:
            page_no = 1

        candidate = self._build_interleaved_beverage_dessert_items(raw_text, page_no=page_no)
        if not candidate:
            return parsed, False

        candidate_items = candidate.get("items") or []
        candidate_dessert_names = {norm(x) for x in (candidate.get("dessert_names") or []) if norm(x)}
        parsed_dessert_names = {norm(it.get("name") or "") for it in dessert_items if isinstance(it, dict)}
        parsed_dessert_names.discard("")
        if not candidate_dessert_names or not parsed_dessert_names:
            return parsed, False
        overlap = len(candidate_dessert_names & parsed_dessert_names)
        if overlap < max(3, int(len(parsed_dessert_names) * 0.6)):
            return parsed, False

        candidate_name_set = {
            norm(it.get("name") or "")
            for it in candidate_items
            if isinstance(it, dict) and norm(it.get("name") or "")
        }
        keep: List[Dict[str, Any]] = []
        for item in parsed_items:
            if not isinstance(item, dict):
                continue
            dnorm = dish_norm(item)
            nkey = norm(item.get("name") or "")
            if dnorm in {"beverage", "beverages", "dessert", "desserts"}:
                continue
            if nkey and nkey in candidate_name_set:
                continue
            keep.append(item)

        fixed = dict(parsed)
        fixed["items"] = keep + candidate_items
        return fixed, True

    def _maybe_fix_legend_code_allergens(self, raw_text: str, parsed: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            return parsed, False

        cleaned = self._strip_ui_wrappers(raw_text)
        if not cleaned:
            return parsed, False

        legend: Dict[str, str] = {}
        for m in re.finditer(r"\b([A-Za-z]{1,4})\s*=\s*([A-Za-z][A-Za-z\- ]{1,40})", cleaned):
            code = str(m.group(1) or "").strip().upper()
            label = re.sub(r"\s+", " ", str(m.group(2) or "")).strip(" .,:;")
            if not code or not label:
                continue
            if len(code) > 4:
                continue
            legend[code] = label
        if len(legend) < 3:
            return parsed, False

        legend_codes = set(legend.keys())
        veg_codes = set()
        non_veg_codes = set()
        code_to_allergen_name: Dict[str, str] = {}
        for code, label in legend.items():
            low = re.sub(r"\s+", " ", str(label or "")).strip().lower()
            if low in {"vegetarian", "veg", "vegan"}:
                veg_codes.add(code)
                continue
            if low in {"non veg", "non-veg", "nonveg", "non vegetarian", "non-vegetarian"}:
                non_veg_codes.add(code)
                continue
            code_to_allergen_name[code] = low

        # Build per-page code lines from OCR text (e.g., "V â€¢ D â€¢ E").
        page_code_lines: Dict[int, List[List[str]]] = {}
        current_page = 1
        for raw_line in cleaned.splitlines():
            line = re.sub(r"\s+", " ", str(raw_line or "")).strip()
            if not line:
                continue
            m_pg = re.match(r"^\[PAGE\s+(\d+)\]", line, flags=re.IGNORECASE)
            if m_pg:
                try:
                    current_page = int(m_pg.group(1))
                except Exception:
                    current_page = 1
                continue
            lower = line.lower()
            if lower in {"[body]", "[footer]", "[shared]"}:
                continue
            if "=" in line:
                continue
            has_bullet = ("â€¢" in raw_line) or ("Ã¢â‚¬Â¢" in raw_line)
            if not has_bullet:
                continue
            tokens = [tok.upper() for tok in re.findall(r"\b[A-Za-z]{1,4}\b", line)]
            tokens = [tok for tok in tokens if tok in legend_codes]
            if not tokens:
                continue
            uniq: List[str] = []
            for tok in tokens:
                if tok not in uniq:
                    uniq.append(tok)
            if uniq:
                page_code_lines.setdefault(current_page, []).append(uniq)

        # Group parsed items by page while preserving order.
        page_item_positions: Dict[int, List[int]] = {}
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            page_val = item.get("page")
            try:
                page_no = int(page_val)
            except Exception:
                page_no = 1
            page_item_positions.setdefault(page_no, []).append(idx)

        def extract_codes_from_value(value: Any) -> List[str]:
            if value is None:
                return []
            texts: List[str] = []
            if isinstance(value, list):
                for v in value:
                    s = str(v or "").strip()
                    if s:
                        texts.append(s)
            else:
                s = str(value or "").strip()
                if s:
                    texts.append(s)
            if not texts:
                return []
            found: List[str] = []
            for text in texts:
                toks = [tok.upper() for tok in re.findall(r"[A-Za-z]{1,10}", text)]
                for tok in toks:
                    if tok in legend_codes and tok not in found:
                        found.append(tok)
            return found

        def is_code_only_value(value: Any) -> bool:
            if value is None:
                return False
            texts: List[str] = []
            if isinstance(value, list):
                for v in value:
                    s = str(v or "").strip()
                    if s:
                        texts.append(s)
            else:
                s = str(value or "").strip()
                if s:
                    texts.append(s)
            if not texts:
                return False
            words = [w.upper() for text in texts for w in re.findall(r"[A-Za-z]{1,10}", text)]
            if not words:
                return False
            return all(w in legend_codes for w in words)

        changed = False
        for page_no, positions in page_item_positions.items():
            line_codes = page_code_lines.get(page_no, [])

            best_shift = 0
            if line_codes and positions:
                best_key = None
                for shift in range(-len(positions), len(line_codes) + 1):
                    overlap_score = 0
                    missing_mapped = 0
                    mapped_count = 0
                    for pos_idx, item_idx in enumerate(positions):
                        li = pos_idx + shift
                        mapped_codes = line_codes[li] if 0 <= li < len(line_codes) else []
                        if mapped_codes:
                            mapped_count += 1
                        item = items[item_idx]
                        if not isinstance(item, dict):
                            continue
                        current_val = item.get("allergens")
                        current_codes = extract_codes_from_value(current_val)
                        if current_codes and mapped_codes:
                            overlap_score += len(set(current_codes) & set(mapped_codes))
                        missing = (
                            current_val is None
                            or (isinstance(current_val, str) and not current_val.strip())
                            or (isinstance(current_val, list) and len(current_val) == 0)
                        )
                        if missing and mapped_codes:
                            missing_mapped += 1
                    key = (overlap_score, missing_mapped, mapped_count, -abs(shift))
                    if best_key is None or key > best_key:
                        best_key = key
                        best_shift = shift

            for pos_idx, item_idx in enumerate(positions):
                item = items[item_idx]
                if not isinstance(item, dict):
                    continue
                mapped_codes: List[str] = []
                li = pos_idx + best_shift
                if 0 <= li < len(line_codes):
                    mapped_codes = list(line_codes[li])

                current_val = item.get("allergens")
                missing = (
                    current_val is None
                    or (isinstance(current_val, str) and not current_val.strip())
                    or (isinstance(current_val, list) and len(current_val) == 0)
                )
                code_only = is_code_only_value(current_val)
                if not (missing or code_only):
                    continue

                current_codes = extract_codes_from_value(current_val)
                merged_codes: List[str] = []
                for c in current_codes + mapped_codes:
                    if c in legend_codes and c not in merged_codes:
                        merged_codes.append(c)

                if not merged_codes and missing:
                    item["allergens"] = []
                    changed = True
                    continue
                if not merged_codes:
                    continue

                if item.get("veg") is None and any(c in veg_codes for c in merged_codes):
                    item["veg"] = True
                if item.get("non_veg") is None and any(c in non_veg_codes for c in merged_codes):
                    item["non_veg"] = True

                names: List[str] = []
                for c in merged_codes:
                    if c in veg_codes or c in non_veg_codes:
                        continue
                    nm = code_to_allergen_name.get(c)
                    if nm and nm not in names:
                        names.append(nm)
                if item.get("allergens") != names:
                    item["allergens"] = names
                    changed = True

        if not changed:
            return parsed, False
        fixed = dict(parsed)
        fixed["items"] = items
        return fixed, True

    def _coerce_bool_like(self, value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
        if not text:
            return None
        if text in {"1", "true", "yes", "y", "veg", "vegetarian", "nonveg", "non-veg", "non vegetarian"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
        return None

    def _allergen_list_from_value(self, value: Any) -> List[str]:
        entries: List[str] = []
        if isinstance(value, list):
            entries = [str(v or "") for v in value]
        elif value is not None:
            text = str(value or "")
            text = text.replace("&", ",")
            entries = re.split(r"[,/;|]+", text)
        out: List[str] = []
        allowed = set(ALLOWED_LABELS) - {"veg", "non_veg"}
        for raw in entries:
            seg = re.sub(r"\s+", " ", str(raw or "")).strip().lower()
            if not seg:
                continue
            toks = [t for t in re.split(r"[^a-z0-9]+", seg) if t]
            mapped = None
            if seg in TOKEN_SYNONYMS:
                mapped = TOKEN_SYNONYMS[seg]
            elif len(toks) == 2 and tuple(toks) in PHRASE_SYNONYMS:
                mapped = PHRASE_SYNONYMS[tuple(toks)]
            elif len(toks) == 3 and tuple(toks) in PHRASE_SYNONYMS:
                mapped = PHRASE_SYNONYMS[tuple(toks)]
            else:
                for tok in toks:
                    if tok in TOKEN_SYNONYMS:
                        mapped = TOKEN_SYNONYMS[tok]
                        break
            cand = mapped or seg
            if cand in {"veg", "non_veg"}:
                continue
            if cand in allowed and cand not in out:
                out.append(cand)
        return out

    def _coerce_parsed_icon_fields(
        self,
        parsed: Dict[str, Any],
        icon_lines: List[Dict[str, Any]] | None,
        raw_text: str | None = None,
    ) -> Tuple[Dict[str, Any], bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        if not isinstance(icon_lines, list) or not icon_lines:
            return parsed, False
        items = parsed.get("items")
        if not isinstance(items, list):
            return parsed, False

        def norm_name(s: str) -> str:
            text = str(s or "").lower()
            # Remove common OCR suffix fragments used in menu lines (kcal/price markers).
            text = re.sub(r"\(kcal[^)]*\)", " ", text)
            text = re.sub(r"\bkcal\s*\d{1,4}\b", " ", text)
            text = re.sub(r"[\~\-â€“â€”]?\s*\d{2,6}(?:[.,]\d{1,2})?\s*$", " ", text)
            return re.sub(r"[^a-z0-9]+", "", text)

        def _item_has_kcal_value(item_obj: Dict[str, Any]) -> bool:
            raw_k = str(item_obj.get("kcal") or "").strip()
            if not raw_k:
                return False
            if self._normalize_kcal(raw_k) is not None:
                return True
            return re.fullmatch(r"\d{1,4}(?:[.,]\d{1,2})?", raw_k) is not None

        indexed_icons: Dict[int, List[Tuple[str, List[str], bool]]] = {}
        if isinstance(icon_lines, list):
            page_entries: Dict[int, List[Dict[str, Any]]] = {}
            for line in icon_lines:
                if not isinstance(line, dict):
                    continue
                try:
                    page = int(line.get("page"))
                except Exception:
                    continue
                txt = str(line.get("text") or "")
                n = norm_name(txt)
                if not n:
                    continue
                icons = [str(i).strip().lower() for i in (line.get("icons") or []) if str(i or "").strip()]
                if not icons:
                    continue
                uniq_icons: List[str] = []
                for ic in icons:
                    if ic not in uniq_icons:
                        uniq_icons.append(ic)
                bbox = line.get("bbox")
                x0 = float("inf")
                y0 = float("inf")
                if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
                    try:
                        x0 = float(bbox[0])
                        y0 = float(bbox[1])
                    except Exception:
                        x0 = float("inf")
                        y0 = float("inf")
                page_entries.setdefault(page, []).append(
                    {
                        "key": n,
                        "icons": uniq_icons,
                        "x0": x0,
                        "y0": y0,
                    }
                )

            for page, entries in page_entries.items():
                entries.sort(key=lambda e: (e.get("y0", float("inf")), e.get("x0", float("inf"))))
                primary_flags = [
                    ("veg" in (e.get("icons") or []) or "non_veg" in (e.get("icons") or []))
                    for e in entries
                ]
                out: List[Tuple[str, List[str], bool]] = []
                for idx, entry in enumerate(entries):
                    key = str(entry.get("key") or "")
                    icons = [str(ic) for ic in (entry.get("icons") or [])]
                    combined_icons: List[str] = []
                    for ic in icons:
                        if ic not in combined_icons:
                            combined_icons.append(ic)

                    # In many menus, allergens are on a following kcal line.
                    # Merge non-primary icon lines until the next primary dish line.
                    if primary_flags[idx]:
                        j = idx + 1
                        while j < len(entries) and not primary_flags[j]:
                            for ic in (entries[j].get("icons") or []):
                                ic_s = str(ic)
                                if ic_s and ic_s not in combined_icons:
                                    combined_icons.append(ic_s)
                            j += 1

                    out.append((key, combined_icons, primary_flags[idx]))
                indexed_icons[page] = out

        has_icon_context = True
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            name_key = norm_name(item.get("name") or "")
            if not name_key:
                continue
            try:
                page_no = int(item.get("page"))
            except Exception:
                page_no = 1

            match_icons: List[str] = []
            best_ratio_any = 0.0
            best_icons_any: List[str] = []
            best_ratio_primary = 0.0
            best_icons_primary: List[str] = []
            for line_key, line_icons, is_primary in indexed_icons.get(page_no, []):
                long_name_containment = (len(name_key) >= 6 and name_key in line_key)
                long_line_containment = (len(line_key) >= 6 and line_key in name_key)
                if long_name_containment or long_line_containment:
                    ratio = max(0.88, min(len(name_key), len(line_key)) / max(len(name_key), len(line_key)))
                else:
                    ratio = SequenceMatcher(None, name_key, line_key).ratio()
                if ratio > best_ratio_any:
                    best_ratio_any = ratio
                    best_icons_any = line_icons
                if is_primary and ratio > best_ratio_primary:
                    best_ratio_primary = ratio
                    best_icons_primary = line_icons

            if best_ratio_primary >= 0.66:
                match_icons = best_icons_primary
            elif best_ratio_any >= 0.72:
                match_icons = best_icons_any

            # Some OCR lines carry a parent heading and child variant separately.
            # If an item name contains multiple icon-line name fragments, merge them.
            contained_matches: List[List[str]] = []
            for line_key, line_icons, _is_primary in indexed_icons.get(page_no, []):
                if not line_key or len(line_key) < 6:
                    continue
                if line_key in name_key:
                    contained_matches.append([str(ic) for ic in (line_icons or [])])
            if len(contained_matches) >= 2:
                merged_contained: List[str] = []
                for icon_set in contained_matches:
                    for ic in icon_set:
                        if ic and ic not in merged_contained:
                            merged_contained.append(ic)
                if merged_contained:
                    match_icons = merged_contained

            base_allergens = self._allergen_list_from_value(item.get("allergens"))
            icon_allergens = [ic for ic in match_icons if ic not in {"veg", "non_veg"}]
            # If we have a very strong line match and that line carries only veg/non-veg markers,
            # prefer icon authority and clear leaked allergen text.
            if match_icons and not icon_allergens and best_ratio_primary >= 0.9:
                base_allergens = []

            # Guard against legend-wide allergen leakage on dense menus when no icon match exists.
            if not match_icons and len(base_allergens) >= 5:
                text_probe_for_cleanup = {
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "allergens": [],
                }
                text_probe_for_cleanup = self._augment_allergens_from_text(text_probe_for_cleanup)
                inferred_cleanup = [
                    str(a)
                    for a in (text_probe_for_cleanup.get("allergens") or [])
                    if str(a)
                ]
                if inferred_cleanup and set(inferred_cleanup).issubset(set(base_allergens)):
                    base_allergens = inferred_cleanup

            # If the item-to-icon match is strong, icon allergens should be authoritative.
            strict_icon_override = (
                (best_ratio_primary >= 0.9 and bool(icon_allergens))
                or (best_ratio_primary < 0.66 and best_ratio_any >= 0.95 and bool(icon_allergens))
                or (len(contained_matches) >= 2 and bool(icon_allergens))
            )
            merged_allergens: List[str] = []
            if strict_icon_override:
                merged_allergens = list(icon_allergens)
            else:
                # If GPT leaked a legend-wide allergen string into one item, prefer matched icon allergens.
                if len(base_allergens) >= 8 and icon_allergens:
                    base_allergens = []
                for a in base_allergens + icon_allergens:
                    if a not in merged_allergens:
                        merged_allergens.append(a)

            # Targeted text-derived supplement for under-detected seafood classes.
            # This keeps icon authority while still recovering labels like "shellfish"
            # from names such as "bivalvia" when icon extraction misses that one label.
            text_probe = {
                "name": item.get("name"),
                "description": item.get("description"),
                "allergens": [],
            }
            text_probe = self._augment_allergens_from_text(text_probe)
            text_allergens = [str(a) for a in (text_probe.get("allergens") or []) if str(a)]
            item_has_kcal = _item_has_kcal_value(item)
            if item_has_kcal or bool(match_icons):
                for extra in text_allergens:
                    if extra in {"shellfish", "molluscs", "crustacean"} and extra not in merged_allergens:
                        merged_allergens.append(extra)

            raw_veg = self._coerce_bool_like(item.get("veg"))
            raw_non = self._coerce_bool_like(item.get("non_veg"))
            if match_icons:
                has_veg = "veg" in match_icons
                has_non = "non_veg" in match_icons
                nonveg_hint = bool(
                    re.search(
                        r"\b(chicken|mutton|lamb|goat|duck|pork|beef|bacon|ham|fish|seafood|prawn|shrimp|crab|lobster)\b",
                        f"{item.get('name','')} {item.get('description','')}".lower(),
                    )
                )
                if has_veg and not has_non:
                    if nonveg_hint:
                        raw_veg = False
                        if raw_non is None:
                            raw_non = True
                    else:
                        raw_veg = True
                        raw_non = False
                elif has_non and not has_veg:
                    raw_non = True
                    raw_veg = False
                else:
                    if raw_veg is None and has_veg:
                        raw_veg = True
                    if raw_non is None and has_non:
                        raw_non = True

            if item.get("allergens") != merged_allergens:
                item["allergens"] = merged_allergens
                changed = True
            if item.get("veg") != raw_veg:
                item["veg"] = raw_veg
                changed = True
            if item.get("non_veg") != raw_non:
                item["non_veg"] = raw_non
                changed = True

        def _item_has_kcal_value(it: Dict[str, Any]) -> bool:
            raw_k = str(it.get("kcal") or "").strip()
            if not raw_k:
                return False
            if self._normalize_kcal(raw_k) is not None:
                return True
            return re.fullmatch(r"\d{1,4}(?:[.,]\d{1,2})?", raw_k) is not None

        dense_context = False
        if raw_text:
            raw_low = self._strip_ui_wrappers(raw_text).lower()
            dense_context = self._looks_like_dense_calorie_raw_text(raw_text)
            if not dense_context:
                dense_context = (
                    raw_low.count("signature dish") >= 2
                    and raw_low.count("crustacean") >= 2
                    and raw_low.count("kcal") >= 8
                )

        kcal_rich_menu = sum(
            1
            for it in items
            if isinstance(it, dict) and _item_has_kcal_value(it)
        ) >= 8
        if kcal_rich_menu and has_icon_context and dense_context:
            # Propagate allergens across obvious variant rows that share the same base description.
            # Example: one heading dish plus multiple variants listed below it.
            variant_groups: Dict[Tuple[int, str, str], List[Dict[str, Any]]] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                desc = re.sub(r"\s+", " ", str(item.get("description") or "").strip().lower())
                if not desc:
                    continue
                desc_parts = [p.strip() for p in desc.split(",") if p.strip()]
                if len(desc_parts) >= 3:
                    desc_key = ",".join(desc_parts[:3]).strip()
                else:
                    desc_key = desc
                if len(desc_key) < 12:
                    continue
                try:
                    pg = int(item.get("page"))
                except Exception:
                    pg = 1
                dish = str(item.get("dish_type") or "").strip().lower()
                variant_groups.setdefault((pg, dish, desc_key), []).append(item)

            for _, group_items in variant_groups.items():
                if len(group_items) < 2:
                    continue
                union_allergens: List[str] = []
                for it in group_items:
                    for a in self._allergen_list_from_value(it.get("allergens")):
                        if a not in union_allergens:
                            union_allergens.append(a)
                if not union_allergens:
                    continue
                for it in group_items:
                    cur = self._allergen_list_from_value(it.get("allergens"))
                    if not cur:
                        it["allergens"] = list(union_allergens)
                        changed = True
                        continue
                    if set(cur).issubset(set(union_allergens)) and len(cur) < len(union_allergens):
                        merged_cur = list(cur)
                        for a in union_allergens:
                            if a not in merged_cur:
                                merged_cur.append(a)
                        it["allergens"] = merged_cur
                        changed = True

            # Fill short runs of missing allergens from nearby items on the same page.
            # This helps when icon OCR misses one or two consecutive lines in dense menus.
            idx = 0
            while idx < len(items):
                cur = items[idx] if idx < len(items) else None
                if not isinstance(cur, dict):
                    idx += 1
                    continue
                try:
                    cur_page = int(cur.get("page"))
                except Exception:
                    cur_page = 1
                cur_name_key = re.sub(r"[^a-z0-9]+", "", str(cur.get("name") or "").lower())
                if cur_name_key in {"steamedrice", "plainrice"}:
                    idx += 1
                    continue
                if self._allergen_list_from_value(cur.get("allergens")):
                    idx += 1
                    continue
                run_start = idx
                run_end = idx
                while run_end + 1 < len(items):
                    nxt = items[run_end + 1]
                    if not isinstance(nxt, dict):
                        break
                    try:
                        nxt_page = int(nxt.get("page"))
                    except Exception:
                        nxt_page = 1
                    if nxt_page != cur_page:
                        break
                    if self._allergen_list_from_value(nxt.get("allergens")):
                        break
                    run_end += 1

                prev_sets: List[List[str]] = []
                back = run_start - 1
                while back >= 0 and len(prev_sets) < 2:
                    prv = items[back]
                    if isinstance(prv, dict):
                        try:
                            prv_page = int(prv.get("page"))
                        except Exception:
                            prv_page = 1
                        if prv_page == cur_page:
                            al = self._allergen_list_from_value(prv.get("allergens"))
                            if al:
                                prev_sets.append(al)
                        elif prev_sets:
                            break
                    back -= 1

                if prev_sets:
                    left1 = prev_sets[0]
                    left2 = prev_sets[1] if len(prev_sets) > 1 else []
                    for pos in range(run_start, run_end + 1):
                        it = items[pos]
                        if not isinstance(it, dict):
                            continue
                        if self._allergen_list_from_value(it.get("allergens")):
                            continue
                        fill = list(left1)
                        if pos == run_start and left2:
                            overlap = len(set(left1) & set(left2))
                            if overlap >= 2:
                                merged_fill: List[str] = []
                                for a in left1 + left2:
                                    if a not in merged_fill:
                                        merged_fill.append(a)
                                fill = merged_fill
                        if fill:
                            it["allergens"] = fill
                            changed = True
                idx = run_end + 1

            # Final targeted fallback for remaining empty-allergen dense items.
            # Run after neighbor propagation so section context is preferred first.
            for it in items:
                if not isinstance(it, dict):
                    continue
                if self._allergen_list_from_value(it.get("allergens")):
                    continue
                if not _item_has_kcal_value(it):
                    continue
                probe = {"name": it.get("name"), "description": it.get("description"), "allergens": []}
                probe = self._augment_allergens_from_text(probe)
                inferred = [str(a) for a in (probe.get("allergens") or []) if str(a)]
                if inferred:
                    it["allergens"] = inferred
                    changed = True

        if not changed:
            return parsed, False
        fixed = dict(parsed)
        fixed["items"] = items
        return fixed, True

    def _should_retry_dense_calorie_parse(self, raw_text: str, parsed: Dict) -> bool:
        if not isinstance(parsed, dict):
            return False
        items = parsed.get("items")
        if not isinstance(items, list) or len(items) < 8:
            return False
        if not self._looks_like_dense_calorie_raw_text(raw_text):
            return False

        collapsed = 0
        long_names = 0
        missing_price = 0
        total = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            total += 1
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if name.lower().count("cal.") >= 2:
                collapsed += 1
            if len(name) >= 100:
                long_names += 1
            price = str(item.get("price") or "").strip()
            if not self._normalize_price(price):
                missing_price += 1
        if total < 8:
            return False
        if collapsed < 2 and long_names < 2:
            return False
        if missing_price < max(3, int(total * 0.3)):
            return False
        return True

    def _looks_like_dense_calorie_raw_text(self, raw_text: str) -> bool:
        raw = self._strip_ui_wrappers(raw_text)
        if not raw:
            return False
        if raw.lower().count("cal.") < 12:
            return False
        if len(re.findall(r"\d+\.\d{2}(?=[A-Za-z0-9])", raw)) < 3:
            return False
        dense_lines = 0
        for line in raw.splitlines():
            text = re.sub(r"\s+", " ", str(line or "")).strip()
            if not text:
                continue
            lower = text.lower()
            if lower.startswith("[page") or lower.startswith("[column") or lower in {"[shared]", "[body]", "[footer]"}:
                continue
            if lower.count("cal.") >= 2 and len(text) >= 90:
                dense_lines += 1
        return dense_lines >= 2

    def _has_dense_special_sections(self, parsed: Dict) -> bool:
        # Deprecated narrow-section detector retained for compatibility.
        # Disabled to avoid domain-specific assumptions in alignment logic.
        return False

    def _rewrite_dense_calorie_raw_text(self, raw_text: str) -> str:
        cleaned = self._strip_ui_wrappers(raw_text)
        if not cleaned:
            return cleaned
        out_lines: List[str] = []
        for raw_line in cleaned.splitlines():
            line = re.sub(r"\s+", " ", str(raw_line or "")).strip()
            if not line:
                continue
            lower = line.lower()
            if lower.startswith("[page") or lower.startswith("[column") or lower in {"[shared]", "[body]", "[footer]"}:
                out_lines.append(line)
                continue
            line = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", line)
            line = re.sub(r"(?<=[A-Za-z])(?=\d{2,4}\s*cal\.)", " ", line)
            line = re.sub(r"(cal\.\))\s*(?=[A-Z])", r"\1\n", line)
            line = re.sub(r"(\.?\d+\.\d{2})\s*(?=(?:[A-Z]|\d+\s*[A-Z]))", r"\1\n", line)
            line = re.sub(r"(cal\.)\s*(?=[A-Z]{3,})", r"\1\n", line)
            for part in line.splitlines():
                part = re.sub(r"\s+", " ", part).strip()
                if part:
                    out_lines.append(part)
        return "\n".join(out_lines).strip()

    def _openai_parsed_quality_score(self, parsed: Dict) -> float:
        if not isinstance(parsed, dict):
            return -1.0
        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            return -1.0

        score = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                score -= 0.5
                continue
            price = str(item.get("price") or "").strip()
            norm_price = self._normalize_price(price)
            cal_count = name.lower().count("cal.")

            score += 1.0
            if norm_price:
                score += 0.8
            else:
                score -= 0.4
            if cal_count >= 2:
                score -= 1.4
            if len(name) >= 100:
                score -= 1.2
            if len(name.split()) <= 1 and not norm_price:
                score -= 0.3
        return score

    def _should_align_with_deterministic_output(self, formatted: Dict, deterministic: Dict) -> bool:
        det_items = deterministic.get("items") if isinstance(deterministic, dict) else None
        fmt_items = formatted.get("items") if isinstance(formatted, dict) else None
        if not isinstance(det_items, list) or not det_items:
            return False
        if not isinstance(fmt_items, list) or not fmt_items:
            return True

        det_quality = self._openai_parsed_quality_score({"items": det_items})
        fmt_quality = self._openai_parsed_quality_score({"items": fmt_items})
        if det_quality + 2.0 < fmt_quality:
            return False

        def collapsed_count(items: List[Dict[str, Any]]) -> int:
            count = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                if name.lower().count("cal.") >= 2 or len(name) >= 100:
                    count += 1
            return count

        det_collapsed = collapsed_count(det_items)
        fmt_collapsed = collapsed_count(fmt_items)
        if det_collapsed > (fmt_collapsed + 2):
            return False
        return True

    def _extract_dense_menu_missing_sections(self, raw_text: str, page_no: int | None = 1) -> Dict[str, List[Dict[str, Any]]]:
        sections: Dict[str, List[Dict[str, Any]]] = {}
        cleaned = self._strip_ui_wrappers(raw_text)
        if not cleaned:
            return sections

        lines = [re.sub(r"\s+", " ", str(ln or "")).strip() for ln in cleaned.splitlines()]
        lines = [ln for ln in lines if ln]
        if not lines:
            return sections

        def is_marker(line: str) -> bool:
            low = line.lower()
            return low.startswith("[page") or low.startswith("[column") or low in {"[shared]", "[body]", "[footer]"}

        def clean_name(text: str) -> str:
            nm = self._clean_item_name(re.sub(r"\s+", " ", str(text or "")).strip())
            nm = re.sub(r"\s+\bGS\b$", "", nm, flags=re.IGNORECASE).strip()
            return nm.strip(" -,.;:")

        def clean_heading(text: str) -> str:
            hd = re.sub(r"\s+", " ", str(text or "")).strip()
            hd = re.sub(r"^[\*\-:|.]+", "", hd).strip()
            hd = re.sub(r"[\*\-:|.]+$", "", hd).strip()
            hd = re.sub(r"\([^)]*\)", "", hd).strip()
            return hd

        def heading_like(text: str) -> bool:
            hd = clean_heading(text)
            if not hd:
                return False
            if len(hd) > 64:
                return False
            low = hd.lower()
            if re.search(r"\b\d{1,4}(?:-\d{1,4})?\s*cal\.", low):
                return False
            words = re.findall(r"[A-Za-z]+", hd)
            if len(words) < 1 or len(words) > 9:
                return False
            if len(words) == 1 and len(hd) < 4:
                return False
            alpha = sum(ch.isalpha() for ch in hd)
            if alpha < 3:
                return False
            return True

        def item_template(
            name: str,
            price: str | None,
            kcal: str | None,
            dish_type: str,
            description: str | None = None,
        ) -> Dict[str, Any]:
            return {
                "name": name,
                "price": price,
                "kcal": kcal,
                "description": description,
                "allergens": None,
                "veg": None,
                "non_veg": None,
                "page": page_no,
                "dish_type": dish_type,
                "timings": None,
            }

        def split_heading_and_payload(line: str) -> Tuple[str | None, str]:
            text_line = re.sub(r"\s+", " ", str(line or "")).strip()
            if not text_line:
                return None, ""

            item_start = None
            patterns = (
                r"([A-Z][A-Z0-9 '&\-\*]{2,})\s+(?:GS\s+)?\d{1,4}(?:-\d{1,4})?\s*cal\.",
                r"([A-Z][A-Z0-9 '&\-\*]{2,})\s+(?:GS\s+)?Subtract\s+\d{1,4}(?:/\d{1,4})?\s*cal\.",
            )
            for pat in patterns:
                m = re.search(pat, text_line)
                if not m:
                    continue
                s = m.start(1)
                if item_start is None or s < item_start:
                    item_start = s

            if item_start is None:
                hd = clean_heading(text_line)
                if heading_like(hd):
                    return hd, ""
                return None, text_line

            head_raw = text_line[:item_start].strip()
            payload = text_line[item_start:].strip()
            hd = clean_heading(head_raw)
            if heading_like(hd):
                return hd, payload
            return None, text_line

        section_buffers: Dict[str, List[str]] = {}
        section_default_price: Dict[str, str | None] = {}
        section_order: List[str] = []
        current_heading: str | None = None

        for line in lines:
            if is_marker(line):
                current_heading = None
                continue

            heading, payload = split_heading_and_payload(line)
            if heading:
                current_heading = heading
                if heading not in section_buffers:
                    section_buffers[heading] = []
                    section_order.append(heading)
                    section_default_price[heading] = None
                m_price = re.search(r"for\s+(\d*\.\d{2})", line, flags=re.IGNORECASE)
                if m_price:
                    section_default_price[heading] = m_price.group(1)
                if payload:
                    section_buffers[heading].append(payload)
                continue

            if current_heading:
                section_buffers.setdefault(current_heading, []).append(line)

        for heading in section_order:
            payload_lines = section_buffers.get(heading) or []
            if not payload_lines:
                continue
            blob = " ".join(payload_lines).strip()
            if not blob:
                continue

            out_items: List[Dict[str, Any]] = []
            seen_names: set[str] = set()
            default_price = section_default_price.get(heading)

            for name, kcal, price in re.findall(
                r"([A-Z][A-Z0-9 '&\-\*]{2,})\s+(?:GS\s+)?(\d{1,4}(?:-\d{1,4})?)\s*cal\.\s*(\d*\.\d{2})?",
                blob,
            ):
                nm = clean_name(name)
                if not nm:
                    continue
                nkey = re.sub(r"[^a-z0-9]+", "", nm.lower())
                if not nkey or nkey in seen_names:
                    continue
                seen_names.add(nkey)
                out_items.append(item_template(nm, price or default_price, kcal, heading))

            for name, kcal in re.findall(
                r"([A-Z][A-Z0-9 '&\-\*]{2,})\s+(?:GS\s+)?Subtract\s+(\d{1,4}(?:/\d{1,4})?)\s*cal\.",
                blob,
                flags=re.IGNORECASE,
            ):
                nm = clean_name(name)
                if not nm:
                    continue
                nkey = re.sub(r"[^a-z0-9]+", "", nm.lower())
                if not nkey or nkey in seen_names:
                    continue
                seen_names.add(nkey)
                out_items.append(item_template(nm, None, kcal, heading, description=f"Subtract {kcal} cal."))

            if out_items:
                sections[heading] = out_items

        return sections

    def _maybe_fix_dense_menu_missing_sections(self, raw_text: str, parsed: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        raw_clean = self._strip_ui_wrappers(raw_text)
        kcal_heavy = (raw_clean.lower().count("(kcal") + raw_clean.lower().count("kcal ")) >= 8
        if not self._looks_like_dense_calorie_raw_text(raw_text) and not kcal_heavy:
            return parsed, False
        parsed_items = parsed.get("items")
        if not isinstance(parsed_items, list) or len(parsed_items) < 20:
            return parsed, False

        page_no = None
        for item in parsed_items:
            if isinstance(item, dict) and item.get("page") is not None:
                page_no = item.get("page")
                break
        if page_no is None:
            page_no = 1

        extracted = self._extract_dense_menu_missing_sections(raw_text, page_no=page_no)
        if not extracted:
            return parsed, False

        def norm(text_val: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(text_val or "").lower())

        pair_set = {
            (norm(it.get("name") or ""), norm(it.get("dish_type") or ""))
            for it in parsed_items
            if isinstance(it, dict)
        }
        dish_set = {norm(it.get("dish_type") or "") for it in parsed_items if isinstance(it, dict)}

        new_items: List[Dict[str, Any]] = []
        for section_name, section_items in extracted.items():
            section_norm = norm(section_name)
            if not section_norm:
                continue
            if section_norm in dish_set:
                continue
            for item in section_items:
                nkey = norm(item.get("name") or "")
                if not nkey:
                    continue
                item_section_norm = norm(item.get("dish_type") or "")
                pair_key = (nkey, item_section_norm)
                if pair_key in pair_set:
                    continue
                new_items.append(item)
                pair_set.add(pair_key)

        if len(new_items) < 2:
            return parsed, False

        fixed = dict(parsed)
        fixed["items"] = list(parsed_items) + new_items

        footer_val = fixed.get("footer_text")
        if footer_val is not None:
            footer_lines: List[str] = []
            footer_was_list = isinstance(footer_val, list)
            if footer_was_list:
                for v in footer_val:
                    for ln in str(v or "").splitlines():
                        s = re.sub(r"\s+", " ", ln).strip()
                        if s:
                            footer_lines.append(s)
            else:
                for ln in str(footer_val or "").splitlines():
                    s = re.sub(r"\s+", " ", ln).strip()
                    if s:
                        footer_lines.append(s)

            remove_heads = {
                re.sub(r"\s+", " ", str(it.get("dish_type") or "")).strip().lower()
                for it in new_items
                if isinstance(it, dict) and str(it.get("dish_type") or "").strip()
            }
            promoted_names = {
                norm(it.get("name") or "")
                for it in new_items
                if isinstance(it, dict) and str(it.get("name") or "").strip()
            }

            kept_lines: List[str] = []
            for ln in footer_lines:
                low = ln.lower().strip()
                if not low:
                    continue
                if re.fullmatch(r"\*+", low):
                    continue
                if any(h and (h == low or low.startswith(h)) for h in remove_heads):
                    continue
                if "subtract" in low and (" cal." in low or low.endswith("cal")) and remove_heads:
                    continue
                nline = norm(ln)
                if nline and any(pn and pn in nline for pn in promoted_names):
                    continue
                kept_lines.append(ln)

            if footer_was_list:
                fixed["footer_text"] = kept_lines
            else:
                fixed["footer_text"] = ("\n".join(kept_lines) if kept_lines else None)
        return fixed, True

    def _maybe_fix_dense_grouped_variants(self, raw_text: str, parsed: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        parsed_items = parsed.get("items")
        if not isinstance(parsed_items, list) or not parsed_items:
            return parsed, False

        raw_clean = self._strip_ui_wrappers(raw_text)
        if not raw_clean:
            return parsed, False
        if not self._looks_like_dense_calorie_raw_text(raw_text) and raw_clean.lower().count("(kcal") < 6:
            return parsed, False

        pages = self._split_raw_text_pages(raw_text)
        if not pages:
            return parsed, False

        items: List[Dict[str, Any]] = [it for it in parsed_items if isinstance(it, dict)]
        changed = False

        def norm(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

        def page_of(item: Dict[str, Any]) -> int:
            try:
                return int(item.get("page"))
            except Exception:
                return 1

        def clean_line(text: str) -> str:
            return re.sub(r"\s+", " ", str(text or "")).strip()

        def is_heading_candidate(text: str) -> bool:
            line = clean_line(text)
            if not line or len(line) > 72:
                return False
            if "," in line:
                return False
            low = line.lower()
            if "(kcal" in low or "kcal " in low:
                return False
            if self._looks_like_price_line(line):
                return False
            if self._is_footer_text(low):
                return False
            if re.search(r"\d", line):
                return False
            words = re.findall(r"[A-Za-z][A-Za-z'&\-]*", line)
            if len(words) < 1 or len(words) > 9:
                return False
            alpha_chars = [ch for ch in line if ch.isalpha()]
            if len(alpha_chars) < 3:
                return False
            upper_ratio = sum(1 for ch in alpha_chars if ch.isupper()) / max(1, len(alpha_chars))
            return upper_ratio >= 0.6

        def find_idx_exact(page_no: int, name: str) -> int | None:
            nkey = norm(name)
            if not nkey:
                return None
            for idx, item in enumerate(items):
                if page_of(item) != page_no:
                    continue
                if norm(item.get("name") or "") == nkey:
                    return idx
            return None

        def find_idx_contains(page_no: int, token: str) -> int | None:
            tkey = norm(token)
            if not tkey:
                return None
            for idx, item in enumerate(items):
                if page_of(item) != page_no:
                    continue
                if tkey in norm(item.get("name") or ""):
                    return idx
            return None

        def find_idx_exact_any(name: str) -> int | None:
            nkey = norm(name)
            if not nkey:
                return None
            for idx, item in enumerate(items):
                if norm(item.get("name") or "") == nkey:
                    return idx
            return None

        def find_idx_contains_any(token: str) -> int | None:
            tkey = norm(token)
            if not tkey:
                return None
            for idx, item in enumerate(items):
                if tkey in norm(item.get("name") or ""):
                    return idx
            return None

        def page_insert_index(page_no: int, after_idx: int | None = None) -> int:
            if isinstance(after_idx, int):
                return max(0, min(len(items), after_idx + 1))
            page_indices = [i for i, item in enumerate(items) if page_of(item) == page_no]
            if not page_indices:
                return len(items)
            return max(page_indices) + 1

        def first_price_after(lines: List[str], start_idx: int, lookahead: int = 4) -> str | None:
            end = min(len(lines), start_idx + 1 + lookahead)
            for j in range(start_idx + 1, end):
                line = clean_line(lines[j])
                if not line:
                    continue
                if self._looks_like_price_line(line):
                    return self._normalize_price(line)
            return None

        def extract_prices_after(
            lines: List[str],
            start_idx: int,
            max_count: int,
            lookahead: int = 6,
        ) -> List[str]:
            out: List[str] = []
            end = min(len(lines), start_idx + 1 + lookahead)
            for j in range(start_idx + 1, end):
                line = clean_line(lines[j])
                if not line:
                    continue
                if self._looks_like_price_line(line):
                    p = self._normalize_price(line)
                    if p:
                        out.append(p)
                        if len(out) >= max_count:
                            break
                    continue
                if out and re.search(r"[A-Za-z]{3,}", line):
                    break
            return out

        def extract_prices_before(
            lines: List[str],
            start_idx: int,
            max_count: int,
            lookback: int = 4,
        ) -> List[str]:
            out: List[str] = []
            begin = max(0, start_idx - lookback)
            for j in range(start_idx - 1, begin - 1, -1):
                line = clean_line(lines[j])
                if not line:
                    continue
                if self._looks_like_price_line(line):
                    p = self._normalize_price(line)
                    if p:
                        out.append(p)
                        if len(out) >= max_count:
                            break
                    continue
                if out and re.search(r"[A-Za-z]{3,}", line):
                    break
            return out

        def parse_variants_from_line(text: str) -> List[Tuple[str, str]]:
            line = clean_line(text)
            if "(kcal" not in line.lower():
                return []
            if "," in line and line.lower().count("(kcal") <= 1:
                return []
            out: List[Tuple[str, str]] = []
            seen = set()
            for m in re.finditer(r"([A-Z][A-Z0-9 '&\-/]{1,}?)\s*\(kcal\s*(\d{1,4})\)", line, flags=re.IGNORECASE):
                nm = self._clean_item_name(m.group(1) or "")
                nm = re.sub(r"\s+", " ", str(nm or "")).strip(" -,.;:")
                kcal = str(m.group(2) or "").strip()
                nkey = norm(nm)
                if not nkey or nkey in seen:
                    continue
                seen.add(nkey)
                out.append((nm, kcal))
            return out

        def upsert_item(
            *,
            page_no: int,
            name: str,
            dish_type: str | None,
            timings: str | None,
            price: str | None = None,
            kcal: str | None = None,
            description: str | None = None,
            allergens: List[str] | None = None,
            veg: bool | None = None,
            non_veg: bool | None = None,
            after_idx: int | None = None,
        ) -> int:
            nonlocal changed
            idx = find_idx_exact(page_no, name)
            if idx is None:
                item = {
                    "name": name,
                    "price": price,
                    "kcal": kcal,
                    "description": description or "",
                    "allergens": list(allergens or []),
                    "veg": veg,
                    "non_veg": non_veg,
                    "page": page_no,
                    "dish_type": dish_type,
                    "timings": timings,
                }
                ins = page_insert_index(page_no, after_idx=after_idx)
                items.insert(ins, item)
                changed = True
                return ins

            item = items[idx]
            if item.get("name") != name:
                item["name"] = name
                changed = True
            if price and not str(item.get("price") or "").strip():
                item["price"] = price
                changed = True
            if kcal and not str(item.get("kcal") or "").strip():
                item["kcal"] = kcal
                changed = True
            if description and not str(item.get("description") or "").strip():
                item["description"] = description
                changed = True
            if allergens is not None:
                existing = self._allergen_list_from_value(item.get("allergens"))
                incoming = [a for a in allergens if a]
                if incoming and (not existing):
                    item["allergens"] = incoming
                    changed = True
            if item.get("veg") is None and veg is not None:
                item["veg"] = veg
                changed = True
            if item.get("non_veg") is None and non_veg is not None:
                item["non_veg"] = non_veg
                changed = True
            if not item.get("dish_type") and dish_type:
                item["dish_type"] = dish_type
                changed = True
            if not item.get("timings") and timings:
                item["timings"] = timings
                changed = True
            return idx

        for page_no, lines in pages.items():
            i = 0
            while i < len(lines):
                heading_line = clean_line(lines[i])
                if not is_heading_candidate(heading_line):
                    i += 1
                    continue
                heading = self._clean_item_name(heading_line).strip(" -,.;:")
                if not heading:
                    i += 1
                    continue

                variants_by_line: Dict[int, List[Tuple[str, str]]] = {}
                variant_rows: List[Tuple[int, str, str]] = []
                stop_idx = i
                found_variant = False

                for j in range(i + 1, min(len(lines), i + 14)):
                    line = clean_line(lines[j])
                    if not line:
                        continue
                    low = line.lower()
                    if self._is_footer_text(low):
                        break

                    variants = parse_variants_from_line(line)
                    if variants:
                        variants_by_line[j] = variants
                        for name, kcal in variants:
                            variant_rows.append((j, name, kcal))
                        stop_idx = j
                        found_variant = True
                        continue

                    if found_variant:
                        if self._looks_like_price_line(line):
                            stop_idx = j
                            continue
                        if is_heading_candidate(line):
                            break
                        if re.search(r"[A-Za-z]{3,}", line):
                            stop_idx = j
                            continue
                    elif is_heading_candidate(line):
                        break

                variant_keys = {norm(vn) for _, vn, _ in variant_rows if norm(vn)}
                if len(variant_keys) < 2:
                    i += 1
                    continue

                target_page_no = page_no
                heading_item_idx = find_idx_exact(page_no, heading)
                if heading_item_idx is None:
                    heading_item_idx = find_idx_contains(page_no, heading)
                if heading_item_idx is None:
                    heading_item_idx = find_idx_exact_any(heading)
                if heading_item_idx is None:
                    heading_item_idx = find_idx_contains_any(heading)
                if heading_item_idx is None:
                    for _, vn, _ in variant_rows:
                        heading_item_idx = find_idx_exact_any(vn)
                        if heading_item_idx is not None:
                            break
                if heading_item_idx is None:
                    i += 1
                    continue
                target_page_no = page_of(items[heading_item_idx])

                simple_variants = 0
                for _, vn, _ in variant_rows:
                    if "," in vn:
                        continue
                    if len(re.findall(r"[A-Za-z0-9]+", vn)) <= 4:
                        simple_variants += 1
                if simple_variants < 2:
                    i += 1
                    continue

                dish_type = None
                timings = None
                if heading_item_idx is not None:
                    dish_type = items[heading_item_idx].get("dish_type")
                    timings = items[heading_item_idx].get("timings")
                if not dish_type:
                    for it in items:
                        if page_of(it) == target_page_no and str(it.get("dish_type") or "").strip():
                            dish_type = it.get("dish_type")
                            timings = it.get("timings")
                            break

                shared_price = first_price_after(lines, i, lookahead=4)
                price_by_variant: Dict[str, str] = {}
                for line_idx, rows in variants_by_line.items():
                    if len(rows) == 1:
                        nm = rows[0][0]
                        p = first_price_after(lines, line_idx, lookahead=4) or shared_price
                        if p:
                            price_by_variant[norm(nm)] = p
                        continue
                    prices = extract_prices_after(lines, line_idx, max_count=len(rows), lookahead=6)
                    if len(prices) < len(rows):
                        needed = len(rows) - len(prices)
                        prev = extract_prices_before(lines, line_idx, max_count=needed, lookback=4)
                        if prev:
                            prices = list(reversed(prev[:needed])) + prices
                    for pos, (nm, _) in enumerate(rows):
                        p = prices[pos] if pos < len(prices) else shared_price
                        if p:
                            price_by_variant[norm(nm)] = p

                shared_desc = ""
                for j in range(stop_idx + 1, min(len(lines), stop_idx + 5)):
                    cand = clean_line(lines[j])
                    if not cand:
                        continue
                    low = cand.lower()
                    if self._looks_like_price_line(cand):
                        continue
                    if "(kcal" in low or self._is_footer_text(low) or is_heading_candidate(cand):
                        break
                    if re.search(r"[A-Za-z]{3,}", cand):
                        shared_desc = cand.strip(" ,")
                        break

                insert_after = heading_item_idx
                for _, variant_name, variant_kcal in variant_rows:
                    vkey = norm(variant_name)
                    if not vkey:
                        continue
                    full_name = f"{heading} - {variant_name}"

                    full_idx = find_idx_exact(target_page_no, full_name)
                    bare_idx = find_idx_exact(target_page_no, variant_name)
                    base_idx = full_idx if full_idx is not None else bare_idx

                    inherited_allergens: List[str] = []
                    inherited_veg = None
                    inherited_non_veg = None
                    inherited_desc = None
                    inherited_price = None
                    if base_idx is not None:
                        base = items[base_idx]
                        inherited_allergens = self._allergen_list_from_value(base.get("allergens"))
                        inherited_veg = base.get("veg")
                        inherited_non_veg = base.get("non_veg")
                        inherited_desc = str(base.get("description") or "").strip() or None
                        inherited_price = str(base.get("price") or "").strip() or None

                    final_price = inherited_price or price_by_variant.get(vkey) or shared_price
                    final_desc = inherited_desc or shared_desc or None

                    if bare_idx is not None and full_idx is None and bare_idx != heading_item_idx:
                        bare = items[bare_idx]
                        if bare.get("name") != full_name:
                            bare["name"] = full_name
                            changed = True
                        if final_price and not str(bare.get("price") or "").strip():
                            bare["price"] = final_price
                            changed = True
                        if variant_kcal and not str(bare.get("kcal") or "").strip():
                            bare["kcal"] = variant_kcal
                            changed = True
                        if final_desc and not str(bare.get("description") or "").strip():
                            bare["description"] = final_desc
                            changed = True
                        if not bare.get("dish_type") and dish_type:
                            bare["dish_type"] = dish_type
                            changed = True
                        if not bare.get("timings") and timings:
                            bare["timings"] = timings
                            changed = True
                        insert_after = bare_idx
                        continue

                    inserted_idx = upsert_item(
                        page_no=target_page_no,
                        name=full_name,
                        dish_type=dish_type,
                        timings=timings,
                        price=final_price,
                        kcal=variant_kcal,
                        description=final_desc,
                        allergens=inherited_allergens,
                        veg=inherited_veg,
                        non_veg=inherited_non_veg,
                        after_idx=insert_after,
                    )
                    insert_after = inserted_idx

                if heading_item_idx is not None:
                    heading_item = items[heading_item_idx]
                    if shared_desc and not str(heading_item.get("description") or "").strip():
                        heading_item["description"] = shared_desc
                        changed = True
                    if str(heading_item.get("price") or "").strip() and not str(heading_item.get("kcal") or "").strip():
                        priced_children = 0
                        for _, vn, _ in variant_rows:
                            child_idx = find_idx_exact(target_page_no, f"{heading} - {vn}")
                            if child_idx is not None and str(items[child_idx].get("price") or "").strip():
                                priced_children += 1
                        if priced_children >= 2:
                            heading_item["price"] = None
                            changed = True

                i = max(i + 1, stop_idx + 1)

        if not changed:
            return parsed, False
        fixed = dict(parsed)
        fixed["items"] = items
        return fixed, True

    def _split_raw_text_pages(self, raw_text: str) -> Dict[int, List[str]]:
        cleaned = self._strip_ui_wrappers(raw_text)
        if not cleaned:
            return {}
        pages: Dict[int, List[str]] = {}
        current_page = 1
        in_footer = False
        for raw_line in cleaned.splitlines():
            line = re.sub(r"\s+", " ", str(raw_line or "")).strip()
            if not line:
                continue
            m_pg = re.match(r"^\[PAGE\s+(\d+)\]", line, flags=re.IGNORECASE)
            if m_pg:
                try:
                    current_page = int(m_pg.group(1))
                except Exception:
                    current_page = 1
                in_footer = False
                pages.setdefault(current_page, [])
                continue
            lower = line.lower()
            if lower == "[footer]":
                in_footer = True
                continue
            if lower in {"[body]", "[shared]"} or lower.startswith("[column"):
                in_footer = False
                continue
            if in_footer:
                continue
            pages.setdefault(current_page, []).append(line)
        return pages

    def _multi_price_labels_from_line(self, text: str) -> List[str]:
        line = re.sub(r"\s+", " ", str(text or "")).strip().lower()
        if not line:
            return []
        tokens = [t for t in re.split(r"[^a-z0-9]+", line) if t]
        if not tokens:
            return []
        stop_tokens = {"price", "prices", "rate", "rates", "size", "sizes", "per", "by", "the", "of", "in"}
        label_map = {
            "glass": "glass",
            "gl": "glass",
            "bottle": "bottle",
            "bot": "bottle",
            "btl": "bottle",
            "small": "small",
            "sm": "small",
            "medium": "medium",
            "med": "medium",
            "large": "large",
            "lg": "large",
            "half": "half",
            "full": "full",
            "quarter": "quarter",
            "qt": "quarter",
            "pint": "pint",
            "pitcher": "pitcher",
            "carafe": "carafe",
        }
        labels: List[str] = []
        unknown = 0
        for tok in tokens:
            if tok.isdigit():
                continue
            if tok in stop_tokens:
                continue
            mapped = label_map.get(tok)
            if mapped:
                if mapped not in labels:
                    labels.append(mapped)
            else:
                unknown += 1
        if len(labels) >= 2:
            return labels
        if labels and unknown <= 1 and len(tokens) <= 4:
            return labels
        return []

    def _multi_price_labels_near_index(self, lines: List[str], idx: int, lookback: int = 24) -> List[str]:
        start = max(0, idx - lookback)
        labels: List[str] = []
        for j in range(start, idx):
            for label in self._multi_price_labels_from_line(lines[j]):
                if label not in labels:
                    labels.append(label)
        return labels if len(labels) >= 2 else []

    def _looks_like_price_line(self, text: str) -> bool:
        line = re.sub(r"\s+", " ", str(text or "")).strip()
        if not line:
            return False
        if self._normalize_kcal(line):
            return False
        if len(line) > 24:
            return False
        if re.search(r"[A-Za-z]{2,}", line):
            return False
        if not re.search(r"\d{2,6}", line):
            return False
        return self._normalize_price(line) is not None

    def _extract_multi_prices_after_line(
        self,
        lines: List[str],
        idx: int,
        max_count: int = 4,
        max_lookahead: int = 8,
    ) -> List[str]:
        out: List[str] = []
        end = min(len(lines), idx + 1 + max_lookahead)
        for j in range(idx + 1, end):
            line = re.sub(r"\s+", " ", str(lines[j] or "")).strip()
            if not line or re.fullmatch(r"[,.;:\-]+", line):
                continue
            if self._looks_like_price_line(line):
                p = self._normalize_price(line)
                if p:
                    out.append(p)
                    if len(out) >= max_count:
                        break
                continue
            if out and re.search(r"[A-Za-z]", line):
                break
        return out

    def _best_item_line_index(self, lines: List[str], item_name: str, used: set[int] | None = None) -> int | None:
        key = re.sub(r"[^a-z0-9]+", "", str(item_name or "").lower())
        if not key:
            return None
        best_idx = None
        best_ratio = 0.0
        for idx, line in enumerate(lines):
            if used is not None and idx in used:
                continue
            lkey = re.sub(r"[^a-z0-9]+", "", str(line or "").lower())
            if not lkey:
                continue
            if lkey == key:
                return idx
            if key in lkey or lkey in key:
                ratio = max(0.85, min(len(key), len(lkey)) / max(len(key), len(lkey)))
            else:
                ratio = SequenceMatcher(None, key, lkey).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = idx
        if best_idx is not None and best_ratio >= 0.72:
            return best_idx
        return None

    def _maybe_fix_multi_price_columns(self, raw_text: str, parsed: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            return parsed, False
        pages = self._split_raw_text_pages(raw_text)
        if not pages:
            return parsed, False

        changed = False
        used_by_page: Dict[int, set[int]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue

            page_no = None
            try:
                if item.get("page") is not None:
                    page_no = int(item.get("page"))
            except Exception:
                page_no = None

            candidate_pages: List[int]
            if page_no in pages:
                ordered: List[int] = [page_no]
                for off in (1, 2):
                    for cand in (page_no - off, page_no + off):
                        if cand in pages and cand not in ordered:
                            ordered.append(cand)
                candidate_pages = ordered
            else:
                candidate_pages = sorted(pages.keys())

            selected: Tuple[int, int, List[str], List[str]] | None = None
            for pg in candidate_pages:
                lines = pages.get(pg) or []
                if not lines:
                    continue
                used = used_by_page.setdefault(pg, set())
                idx = self._best_item_line_index(lines, name, used)
                if idx is None:
                    continue
                labels = self._multi_price_labels_near_index(lines, idx)
                if len(labels) < 2:
                    continue
                prices = self._extract_multi_prices_after_line(
                    lines,
                    idx,
                    max_count=max(4, len(labels)),
                    max_lookahead=10,
                )
                if len(prices) < 2:
                    continue
                selected = (pg, idx, labels, prices)
                break

            if selected is None:
                continue

            pg, idx, labels, prices = selected
            count = min(len(labels), len(prices))
            if count < 2:
                continue

            price_options: Dict[str, Any] = {}
            for i in range(count):
                label = str(labels[i] or "").strip().lower()
                value = str(prices[i] or "").strip()
                if not label or not value:
                    continue
                if label not in price_options:
                    price_options[label] = value

            if len(price_options) < 2:
                continue

            existing_extra = item.get("extra_attributes")
            merged_extra: Dict[str, Any] = dict(existing_extra) if isinstance(existing_extra, dict) else {}
            existing_options = merged_extra.get("price_options")
            if isinstance(existing_options, dict):
                merged_options: Dict[str, Any] = dict(existing_options)
                for k, v in price_options.items():
                    if k not in merged_options or not str(merged_options.get(k) or "").strip():
                        merged_options[k] = v
                price_options = merged_options

            if merged_extra.get("price_options") != price_options:
                merged_extra["price_options"] = price_options
                item["extra_attributes"] = merged_extra
                changed = True

            if not str(item.get("price") or "").strip():
                first_price = next(iter(price_options.values()), None)
                if first_price is not None:
                    item["price"] = first_price
                    changed = True

            used_by_page.setdefault(pg, set()).add(idx)

        return parsed, changed

    def _maybe_fix_suspicious_dense_prices(self, raw_text: str, parsed: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            return parsed, False
        raw_clean = self._strip_ui_wrappers(raw_text)
        kcal_heavy = (raw_clean.lower().count("(kcal") + raw_clean.lower().count("kcal ")) >= 8
        if not self._looks_like_dense_calorie_raw_text(raw_text) and not kcal_heavy:
            return parsed, False
        pages = self._split_raw_text_pages(raw_text)

        def page_no_of(item: Dict[str, Any]) -> int:
            try:
                return int(item.get("page"))
            except Exception:
                return 1

        def is_beverage_style(dish_type: str) -> bool:
            low = str(dish_type or "").strip().lower()
            if not low:
                return False
            keywords = (
                "wine",
                "beverage",
                "drink",
                "cocktail",
                "mocktail",
                "whiskey",
                "vodka",
                "gin",
                "rum",
                "tequila",
                "champagne",
                "liqueur",
                "beer",
                "coffee",
                "tea",
            )
            return any(k in low for k in keywords)

        # Build page-level signal: pages with mostly 3-digit food prices.
        page_small_price_count: Dict[int, int] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            ptxt = str(item.get("price") or "").strip()
            if not ptxt:
                continue
            pnorm = self._normalize_price(ptxt)
            if not pnorm:
                continue
            if not re.fullmatch(r"\d{2,4}", pnorm):
                continue
            try:
                pval = int(pnorm)
            except Exception:
                continue
            if 100 <= pval <= 999:
                pg = page_no_of(item)
                page_small_price_count[pg] = page_small_price_count.get(pg, 0) + 1

        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_price = str(item.get("price") or "").strip()
            if not raw_price:
                continue

            # If OCR line has a 4-digit glitch token (e.g., 7665) and parsed price is first-3 digits (765),
            # prefer the trailing 3 digits (665), which is the common correction in dense menu scans.
            item_name = str(item.get("name") or "").strip()
            pg = page_no_of(item)
            page_lines = pages.get(pg) or []
            if item_name and page_lines:
                idx = self._best_item_line_index(page_lines, item_name)
                if idx is not None:
                    glitch_token = None
                    end = min(len(page_lines), idx + 7)
                    for j in range(idx + 1, end):
                        ln = re.sub(r"\s+", " ", str(page_lines[j] or "")).strip()
                        if not ln:
                            continue
                        m_glitch = re.fullmatch(r"[6-9]\d{3}", ln)
                        if m_glitch:
                            glitch_token = m_glitch.group(0)
                            break
                        if re.search(r"[A-Za-z]{3,}", ln):
                            # Skip short OCR noise tokens and kcal sub-lines; stop at real prose rows.
                            low_ln = ln.lower()
                            if len(ln) <= 5:
                                continue
                            if "kcal" in low_ln:
                                continue
                            if len(ln.split()) <= 2 and ln.upper() == ln:
                                continue
                            break
                    pnorm = self._normalize_price(raw_price) or ""
                    if glitch_token and re.fullmatch(r"\d{3}", pnorm):
                        first3 = glitch_token[:3]
                        last3 = glitch_token[-3:]
                        deletion_variants = {
                            glitch_token[:3],
                            glitch_token[1:],
                            glitch_token[0] + glitch_token[2:],
                            glitch_token[0:2] + glitch_token[3],
                        }
                        if pnorm in deletion_variants and first3 != last3:
                            try:
                                last_val = int(last3)
                            except Exception:
                                last_val = 0
                            if 100 <= last_val <= 999:
                                item["price"] = last3
                                raw_price = last3
                                changed = True

            # Typical OCR glitch in dense menus: leading "7" merged into a 3-digit price (e.g., 7665 -> 665).
            if not re.fullmatch(r"[6-9]\d{3}", raw_price):
                continue
            if "," in raw_price or "." in raw_price:
                continue
            try:
                raw_val = int(raw_price)
            except Exception:
                continue
            if raw_val < 5000:
                continue
            tail = int(raw_price[-3:])
            if tail < 100 or tail > 999:
                continue
            if is_beverage_style(item.get("dish_type") or ""):
                continue
            if page_small_price_count.get(pg, 0) < 2:
                continue
            if str(item.get("price")) != str(tail):
                item["price"] = str(tail)
                changed = True

        if not changed:
            return parsed, False
        fixed = dict(parsed)
        fixed["items"] = items
        return fixed, True

    def _extract_dietary_marker(self, text: str) -> str | None:
        raw = str(text or "")
        if not raw:
            return None
        matches = re.findall(r"\(([A-Za-z][A-Za-z0-9/&+\-]{0,5})\)", raw)
        for m in matches:
            marker = re.sub(r"\s+", "", str(m or "")).upper()
            if not marker:
                continue
            if 1 <= len(marker) <= 6:
                return marker
        return None

    def _name_without_short_markers(self, name: str) -> str:
        text = str(name or "")
        text = re.sub(r"\(([A-Za-z][A-Za-z0-9/&+\-]{0,5})\)", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _menu_name_is_generic(self, name: str) -> bool:
        value = re.sub(r"\s+", " ", str(name or "")).strip().lower()
        if not value:
            return True
        norm = re.sub(r"[^a-z0-9]+", "", value)
        if not norm:
            return True
        if norm in {
            "menu",
            "page1menu",
            "page2menu",
            "page3menu",
            "beveragemenu",
            "dessertmenu",
            "inroomdining",
        }:
            return True
        if re.fullmatch(r"page\d+menu", norm):
            return True
        if re.fullmatch(r"page\d+", norm):
            return True
        if norm.startswith("page") and "menu" in norm:
            return True
        return False

    def _maybe_fix_menu_name_from_fallback(self, parsed: Dict, fallback: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        hint = str((fallback or {}).get("menu_name") or "").strip()
        if not hint:
            return parsed, False
        parsed_name = str(parsed.get("menu_name") or "").strip()
        should_use_hint = (not parsed_name) or self._menu_name_is_generic(parsed_name)
        if not should_use_hint:
            return parsed, False
        if parsed.get("menu_name") == hint:
            return parsed, False
        fixed = dict(parsed)
        fixed["menu_name"] = hint
        return fixed, True

    def _maybe_fill_dish_type_from_fallback(self, parsed: Dict, fallback: Dict) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        items = parsed.get("items")
        fallback_items = (fallback or {}).get("items") if isinstance(fallback, dict) else None
        if not isinstance(items, list) or not isinstance(fallback_items, list) or not fallback_items:
            return parsed, False

        def norm(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

        by_page_name: Dict[Tuple[int, str], str] = {}

        for it in fallback_items:
            if not isinstance(it, dict):
                continue
            dish_type = str(it.get("dish_type") or "").strip()
            if not dish_type:
                continue
            nkey = norm(it.get("name") or "")
            if not nkey:
                continue
            try:
                page_no = int(it.get("page"))
            except Exception:
                page_no = None
            if page_no is not None and (page_no, nkey) not in by_page_name:
                by_page_name[(page_no, nkey)] = dish_type

        changed = False
        out_items: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                out_items.append(it)
                continue
            base = dict(it)
            current = str(base.get("dish_type") or "").strip()
            if current:
                out_items.append(base)
                continue
            nkey = norm(base.get("name") or "")
            if not nkey:
                out_items.append(base)
                continue
            try:
                page_no = int(base.get("page"))
            except Exception:
                page_no = None

            target = None
            if page_no is not None:
                target = by_page_name.get((page_no, nkey))
            if target:
                base["dish_type"] = target
                changed = True
            out_items.append(base)

        if not changed:
            return parsed, False
        fixed = dict(parsed)
        fixed["items"] = out_items
        return fixed, True

    def _maybe_attach_dietary_markers(
        self,
        parsed: Dict,
        fallback: Dict,
        raw_text: str | None = None,
    ) -> Tuple[Dict, bool]:
        if not isinstance(parsed, dict):
            return parsed, False
        items = parsed.get("items")
        if not isinstance(items, list):
            return parsed, False

        def norm_name(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

        marker_by_name: Dict[str, str] = {}

        # First preference: explicit short markers captured in raw OCR text lines.
        cleaned = self._strip_ui_wrappers(raw_text or "")
        if cleaned:
            raw_lines = [re.sub(r"\s+", " ", str(x or "")).strip() for x in cleaned.splitlines()]
            for raw_line in raw_lines:
                line = re.sub(r"\s+", " ", str(raw_line or "")).strip()
                if not line:
                    continue
                lower = line.lower()
                if lower.startswith("[page") or lower.startswith("[column") or lower in {"[body]", "[footer]", "[shared]"}:
                    continue
                marker = self._extract_dietary_marker(line)
                if not marker:
                    continue
                base = self._name_without_short_markers(line)
                # Remove trailing simple price fragments from the same line if present.
                base = re.sub(r"[\s~\-]*(?:â‚¹|rs\.?|inr|\$)?\s*\d{2,6}(?:[.,]\d{1,2})?\s*$", "", base, flags=re.IGNORECASE)
                nkey = norm_name(base)
                if nkey and nkey not in marker_by_name:
                    marker_by_name[nkey] = marker

        if cleaned:
            for idx, line in enumerate(raw_lines):
                marker = self._extract_dietary_marker(line)
                if not marker:
                    continue
                lower = line.lower()
                if lower.startswith("[page") or lower.startswith("[column") or lower in {"[body]", "[footer]", "[shared]"}:
                    continue
                for back in (1, 2):
                    j = idx - back
                    if j < 0:
                        break
                    prev = str(raw_lines[j] or "").strip()
                    if not prev:
                        continue
                    pl = prev.lower()
                    if pl.startswith("[page") or pl.startswith("[column") or pl in {"[body]", "[footer]", "[shared]"}:
                        continue
                    if self._looks_like_price_line(prev):
                        continue
                    if not re.search(r"[A-Za-z]", prev):
                        continue
                    combined = f"{prev} {line}".strip()
                    base = self._name_without_short_markers(combined)
                    base = re.sub(r"[\s~\-]*(?:Ã¢â€šÂ¹|rs\.?|inr|\$)?\s*\d{2,6}(?:[.,]\d{1,2})?\s*$", "", base, flags=re.IGNORECASE)
                    nkey = norm_name(base)
                    if nkey and nkey not in marker_by_name:
                        marker_by_name[nkey] = marker

        # Fallback: deterministic names (can be noisier on some docs).
        fallback_items = (fallback or {}).get("items") if isinstance(fallback, dict) else None
        if isinstance(fallback_items, list):
            for it in fallback_items:
                if not isinstance(it, dict):
                    continue
                nm = str(it.get("name") or "")
                marker = self._extract_dietary_marker(nm)
                if not marker:
                    continue
                nkey = norm_name(self._name_without_short_markers(nm))
                if nkey and nkey not in marker_by_name:
                    marker_by_name[nkey] = marker

        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue

            marker = self._extract_dietary_marker(name)
            if not marker:
                nkey = norm_name(self._name_without_short_markers(name))
                marker = marker_by_name.get(nkey)
            if not marker:
                # Last fallback: fuzzy match against marker map keys.
                nkey = norm_name(self._name_without_short_markers(name))
                best_ratio = 0.0
                best_marker = None
                for mk, mv in marker_by_name.items():
                    if not nkey or not mk:
                        continue
                    if nkey in mk or mk in nkey:
                        ratio = max(0.82, min(len(nkey), len(mk)) / max(len(nkey), len(mk)))
                    else:
                        ratio = SequenceMatcher(None, nkey, mk).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_marker = mv
                if best_ratio >= 0.84:
                    marker = best_marker
            if not marker:
                continue

            extra = item.get("extra_attributes")
            merged: Dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
            marker_val = str(merged.get("dietary_marker") or "").strip()
            if not marker_val:
                merged["dietary_marker"] = marker
            up = marker.upper()
            if up in {"VG", "VEGAN"}:
                merged.setdefault("vegan", True)
            if up in {"S", "SF", "SEAFOOD"}:
                merged.setdefault("seafood", True)
            if merged != extra:
                item["extra_attributes"] = merged
                changed = True

        if not changed:
            return parsed, False
        fixed = dict(parsed)
        fixed["items"] = items
        return fixed, True

    def _normalize_menu_json(self, parsed: Dict, fallback: Dict) -> Dict:
        def _is_empty_value(v: Any) -> bool:
            if v is None:
                return True
            if isinstance(v, str):
                return not v.strip()
            if isinstance(v, (list, dict, tuple, set)):
                return len(v) == 0
            return False

        top_known_keys = {"menu_name", "items", "other_text", "footer_text", "notes", "extra_sections"}
        extra_sections: Any = None
        if isinstance(parsed.get("extra_sections"), (dict, list)) and not _is_empty_value(parsed.get("extra_sections")):
            extra_sections = parsed.get("extra_sections")
        else:
            extra_map: Dict[str, Any] = {}
            for k, v in parsed.items():
                if k in top_known_keys:
                    continue
                if _is_empty_value(v):
                    continue
                extra_map[str(k)] = v
            if extra_map:
                extra_sections = extra_map

        menu_name = str(parsed.get("menu_name") or "").strip()
        fallback_name = str(fallback.get("menu_name") or "").strip() if isinstance(fallback, dict) else ""
        if fallback_name and (not menu_name or self._menu_name_is_generic(menu_name)):
            menu_name = fallback_name

        out = {
            "menu_name": menu_name or None,
            "items": [],
            "other_text": parsed.get("other_text") if isinstance(parsed.get("other_text"), list) else [],
            "footer_text": parsed.get("footer_text") if isinstance(parsed.get("footer_text"), list) else [],
            "notes": parsed.get("notes") if isinstance(parsed.get("notes"), list) else [],
        }
        if extra_sections is not None:
            out["extra_sections"] = extra_sections
        items = parsed.get("items")
        if not isinstance(items, list):
            return fallback

        for item in items:
            if not isinstance(item, dict):
                continue
            allergens = item.get("allergens")
            if not isinstance(allergens, list):
                allergens = []
            clean_allergens = []
            for a in allergens:
                if a is None:
                    continue
                s = str(a).strip().lower()
                if s and s not in clean_allergens:
                    clean_allergens.append(s)
            raw_price = str(item.get("price") or "").strip()
            normalized_price = self._normalize_price(raw_price) if raw_price else None
            if not normalized_price and raw_price and re.search(r"[a-z]", raw_price.lower()):
                normalized_price = raw_price
            raw_kcal = str(item.get("kcal") or "").strip()
            normalized_kcal = self._normalize_kcal(raw_kcal) if raw_kcal else None
            dish_type = str(item.get("dish_type") or "").strip() or None
            timings = str(item.get("timings") or "").strip() or None
            item_known_keys = {
                "name",
                "price",
                "kcal",
                "description",
                "allergens",
                "veg",
                "non_veg",
                "page",
                "dish_type",
                "timings",
                "extra_attributes",
            }
            extra_attributes: Dict[str, Any] = {}
            if isinstance(item.get("extra_attributes"), dict):
                for k, v in item.get("extra_attributes").items():
                    if _is_empty_value(v):
                        continue
                    extra_attributes[str(k)] = v
            for k, v in item.items():
                if k in item_known_keys:
                    continue
                if _is_empty_value(v):
                    continue
                extra_attributes[str(k)] = v
            if "dietary_marker" not in extra_attributes:
                marker = self._extract_dietary_marker(item.get("name") or "")
                if marker:
                    extra_attributes["dietary_marker"] = marker

            out_item: Dict[str, Any] = {
                "name": (item.get("name") or None),
                "price": normalized_price,
                "kcal": normalized_kcal,
                "description": (item.get("description") or ""),
                "allergens": clean_allergens,
                "veg": item.get("veg") if isinstance(item.get("veg"), bool) else None,
                "non_veg": item.get("non_veg") if isinstance(item.get("non_veg"), bool) else None,
                "page": item.get("page"),
                "dish_type": dish_type,
                "timings": timings,
            }
            if extra_attributes:
                out_item["extra_attributes"] = extra_attributes
            out["items"].append(out_item)

        if not out["items"] and isinstance(fallback.get("items"), list) and fallback.get("items"):
            return fallback
        return out

    def format_menu_deterministic(self, menu_raw: Dict) -> Dict:
        seed = self._build_items_seed(menu_raw)
        return self._format_from_seed(menu_raw, seed)

    def _format_from_seed(self, menu_raw: Dict, seed: List[Dict]) -> Dict:
        items = []
        for entry in seed:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            if self._is_generic_menu_header_text(name):
                continue
            icons = entry.get("icons") or []
            allergens = [i for i in icons if i not in ("veg", "non_veg")]
            item = {
                "name": name,
                "price": entry.get("price") or None,
                "kcal": entry.get("kcal") or None,
                "description": entry.get("description") or "",
                "allergens": allergens,
                "veg": "veg" in icons,
                "non_veg": "non_veg" in icons,
                "page": entry.get("page"),
                "dish_type": entry.get("dish_type") or None,
                "timings": entry.get("timings") or None,
            }
            item = self._augment_allergens_from_text(item)
            items.append(item)
        return {
            "menu_name": None,
            "items": items,
            "other_text": [],
            "footer_text": [],
            "notes": [],
        }

    def _openai_output_ok(self, parsed: Dict, deterministic: Dict) -> bool:
        if not isinstance(parsed, dict):
            return False
        items = parsed.get("items")
        if not isinstance(items, list):
            return False
        det_items = deterministic.get("items") or []
        if det_items:
            ratio = len(items) / max(len(det_items), 1)
            if ratio < 0.8 or ratio > 1.2:
                return False
        # reject outputs that leak footer/legend text into descriptions
        bad_tokens = (
            "kindly inform",
            "all prices are",
            "exclusive of taxes",
            "service charge",
            "calorie content",
        )
        for item in items:
            desc = str(item.get("description") or "").lower()
            if any(tok in desc for tok in bad_tokens):
                return False
        # if OpenAI drops prices/kcal compared to deterministic, reject
        missing_price = 0
        missing_kcal = 0
        total = min(len(items), len(det_items)) if det_items else len(items)
        for i in range(total):
            det = det_items[i] if det_items else {}
            it = items[i] if i < len(items) else {}
            if det.get("price") and not it.get("price"):
                missing_price += 1
            if det.get("kcal") and not it.get("kcal"):
                missing_kcal += 1
        if total and (missing_price / total) > 0.3:
            return False
        if total and (missing_kcal / total) > 0.3:
            return False
        return True

    def _merge_openai_with_seed(self, parsed: Dict, deterministic: Dict) -> Dict:
        det_items = deterministic.get("items") or []
        items = parsed.get("items") or []
        merged = []
        for idx, det in enumerate(det_items):
            base = dict(det)
            if idx < len(items):
                cand = items[idx] or {}
                desc = str(cand.get("description") or "").strip()
                if desc and not self._is_footer_text(desc):
                    base["description"] = desc
                name = str(cand.get("name") or "").strip()
                if name and not self._is_footer_text(name):
                    base["name"] = name
                if not base.get("dish_type"):
                    dish_type = str(cand.get("dish_type") or "").strip()
                    if dish_type:
                        base["dish_type"] = dish_type
                if not base.get("timings"):
                    timings = str(cand.get("timings") or "").strip()
                    if timings:
                        base["timings"] = timings
            base = self._augment_allergens_from_text(base)
            merged.append(base)
        menu_name = parsed.get("menu_name") or deterministic.get("menu_name")
        return {
            "menu_name": menu_name,
            "items": merged,
            "other_text": deterministic.get("other_text", []),
            "footer_text": deterministic.get("footer_text", []),
            "notes": parsed.get("notes") or deterministic.get("notes"),
        }

    def _is_footer_text(self, text: str) -> bool:
        t = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not t:
            return False
        if any(
            key in t
            for key in (
                "kindly inform",
                "all prices are",
                "exclusive of taxes",
                "service charge",
                "calorie content",
                "before you order",
                "some dishes can be made",
                "please let us know",
                "please let us any",
            )
        ):
            return True
        footer_patterns = (
            r"\ballerg(?:y|ies)\b",
            r"\bgluten\s*free\b",
            r"\btaxes?\b.*\b(charged|extra|excluded|exclusive)\b",
            r"\bcharged\s+extra\b",
            r"\bsubject\s+to\b.*\btax\b",
            r"\bprices?\b.*\bexclusive\b",
        )
        for pat in footer_patterns:
            if re.search(pat, t):
                return True
        return False

    def _augment_allergens_from_text(self, item: Dict) -> Dict:
        text = f"{item.get('name','')} {item.get('description','')}".lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
        found = set(item.get("allergens") or [])
        # phrase matching (2-3 tokens)
        for length in (3, 2):
            for i in range(len(tokens) - length + 1):
                phrase = tuple(tokens[i : i + length])
                if phrase in PHRASE_SYNONYMS:
                    lbl = PHRASE_SYNONYMS[phrase]
                    if lbl not in ("veg", "non_veg"):
                        found.add(lbl)
        # token matching
        for token in tokens:
            if token in TOKEN_SYNONYMS:
                lbl = TOKEN_SYNONYMS[token]
                if lbl not in ("veg", "non_veg"):
                    found.add(lbl)
        # keep only allowed allergen labels
        allowed = set(ALLOWED_LABELS) - {"veg", "non_veg"}
        item["allergens"] = [a for a in sorted(found) if a in allowed]
        return item

    def _clean_heading_text(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        cleaned = re.sub(r"^[^A-Za-z0-9]+", "", cleaned).strip()
        cleaned = re.sub(r"[^A-Za-z0-9]+$", "", cleaned).strip()
        return cleaned

    def _extract_time_range(self, text: str) -> str | None:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if not raw:
            return None
        norm = raw.replace("â€“", "-").replace("â€”", "-")
        lower = norm.lower()
        patterns = (
            r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\s*(?:to|-)\s*\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
            r"\b\d{1,2}:\d{2}\s*(?:to|-)\s*\d{1,2}:\d{2}\b",
        )
        for pat in patterns:
            m = re.search(pat, lower, flags=re.IGNORECASE)
            if not m:
                continue
            found = norm[m.start() : m.end()]
            found = re.sub(r"\s+", " ", found).strip()
            return found or None
        return None

    def _extract_page_dish_type(self, lines: List[Dict]) -> str | None:
        keywords = {
            "breakfast",
            "lunch",
            "dinner",
            "soup",
            "soups",
            "brunch",
            "snack",
            "snacks",
            "supper",
            "dessert",
            "desserts",
            "beverage",
            "beverages",
            "drinks",
            "starter",
            "starters",
            "salad",
            "salads",
            "mains",
            "main",
            "appetizer",
            "appetizers",
        }
        best_rank = None
        best_text = None
        for line in lines:
            text = self._clean_heading_text(line.get("name_text") or line.get("text") or "")
            if not text:
                continue
            lower = text.lower()
            if self._extract_time_range(text):
                continue
            if any(tok in lower for tok in ("kindly inform", "all prices are", "service charge", "calorie content")):
                continue
            words = [w for w in re.split(r"[^a-z]+", lower) if w]
            if not words:
                continue
            keyword_count = sum(1 for w in words if w in keywords)
            if keyword_count <= 0:
                continue
            if self._is_generic_menu_header_text(lower):
                continue
            y0 = float((line.get("bbox") or [0, 0, 0, 0])[1])
            rank = (keyword_count, y0, -abs(len(words) - 3))
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_text = text
        return best_text

    def _extract_page_context(self, page: Dict) -> Dict[str, str | None]:
        lines = page.get("lines", []) if isinstance(page, dict) else []
        if not isinstance(lines, list) or not lines:
            return {"dish_type": None, "timings": None}
        ordered = sorted(
            [line for line in lines if isinstance(line, dict)],
            key=lambda l: (l.get("bbox", [0, 0, 0, 0])[1], l.get("bbox", [0, 0, 0, 0])[0]),
        )
        if not ordered:
            return {"dish_type": None, "timings": None}
        page_h = max((line.get("bbox", [0, 0, 0, 0])[3] for line in ordered), default=0.0)
        first_item_y = None
        for line in ordered:
            text = (line.get("name_text") or line.get("text") or "").strip()
            if not text:
                continue
            lower = text.lower()
            if self._extract_time_range(text):
                continue
            if self._is_generic_menu_header_text(lower):
                continue
            icons = line.get("icons") or []
            if icons:
                first_item_y = float((line.get("bbox") or [0, 0, 0, 0])[1])
                break
            if self._extract_price_from_line(line) or self._extract_kcal_from_line(line):
                first_item_y = float((line.get("bbox") or [0, 0, 0, 0])[1])
                break
        heading_cutoff = (page_h * 0.45) if page_h > 0 else float("inf")
        if first_item_y is not None:
            heading_cutoff = min(heading_cutoff, first_item_y - 6.0)
        if heading_cutoff <= 0:
            heading_cutoff = (page_h * 0.45) if page_h > 0 else float("inf")
        heading_lines: List[Dict] = []
        for line in ordered:
            y0 = float((line.get("bbox") or [0, 0, 0, 0])[1])
            if heading_cutoff != float("inf") and y0 > (heading_cutoff + 2.0):
                break
            heading_lines.append(line)
        if not heading_lines:
            heading_lines = ordered[: min(len(ordered), 12)]
        dish_type = self._extract_page_dish_type(heading_lines)
        timings = None
        for line in heading_lines:
            timings = self._extract_time_range(line.get("name_text") or line.get("text") or "")
            if timings:
                break
        if not timings:
            top_cutoff = page_h * 0.6 if page_h > 0 else float("inf")
            for line in ordered:
                y0 = float((line.get("bbox") or [0, 0, 0, 0])[1])
                if y0 > top_cutoff:
                    break
                timings = self._extract_time_range(line.get("name_text") or line.get("text") or "")
                if timings:
                    break
        return {
            "dish_type": dish_type,
            "timings": timings,
        }

    def _propagate_shared_prices(self, items: List[Dict]) -> List[Dict]:
        last_price_by_group: Dict[Tuple[Any, Any, str, str], str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            group = (
                item.get("page"),
                item.get("column_index"),
                str(item.get("dish_type") or "").strip().lower(),
                str(item.get("timings") or "").strip().lower(),
            )
            price = str(item.get("price") or "").strip()
            if price:
                last_price_by_group[group] = price
                continue
            carry = last_price_by_group.get(group)
            if carry:
                item["price"] = carry
        return items

    def _build_items_seed(self, menu_raw: Dict) -> List[Dict]:
        items: List[Dict] = []
        pages = menu_raw.get("pages", [])
        for page in pages:
            lines = page.get("lines", [])
            self._annotate_layout_columns(lines)
            page_context = self._extract_page_context(page)
            page_bottom = max((l.get("bbox", [0, 0, 0, 0])[3] for l in lines), default=0.0)
            legend_top = page_bottom * (1.0 - self.config.footer_band_ratio) if page_bottom > 0 else float("inf")
            columns: Dict[int, List[Dict]] = {}
            for line in lines:
                if not isinstance(line, dict):
                    continue
                role = str(line.get("layout_role") or "").strip().lower()
                if role == "footer":
                    continue
                col_idx = line.get("column_index")
                if col_idx is None:
                    col_idx = 0
                columns.setdefault(int(col_idx), []).append(line)
            column_keys = sorted(columns.keys())
            if any(k > 0 for k in column_keys):
                column_keys = [k for k in column_keys if k > 0]
            for col_idx in column_keys:
                col_lines = self._order_lines_for_seed(columns[col_idx])
                current = None
                in_footer = False
                for idx, line in enumerate(col_lines):
                    text = (line.get("name_text") or line.get("text") or "").strip()
                    if not text:
                        continue
                    role = str(line.get("layout_role") or "").strip().lower()
                    if role == "shared":
                        continue
                    lower = text.lower()
                    y0 = line.get("bbox", [0, 0, 0, 0])[1]
                    if any(
                        key in lower
                        for key in (
                            "kindly inform",
                            "all prices are",
                            "exclusive of taxes",
                            "service charge",
                            "calorie content",
                        )
                    ):
                        in_footer = True
                    if y0 >= legend_top and self._looks_like_legend_line(text):
                        in_footer = True
                    if in_footer:
                        continue
                    is_name = self._is_name_line(line)
                    # Fallback for menus where item names are embedded with kcal or symbols
                    # and don't hit the strict name-line heuristic.
                    if not is_name and self._looks_like_dish_line(text):
                        if self._single_lowercase_name_token(text) is None:
                            is_name = True
                    if is_name:
                        if current:
                            items.append(current)
                        current = {
                            "name": text,
                            "price": self._extract_price_from_line(line),
                            "kcal": self._extract_kcal_from_line(line),
                            "description": [],
                            "icons": line.get("icons", []),
                            "page": page.get("page"),
                            "column_index": col_idx,
                            "dish_type": page_context.get("dish_type"),
                            "timings": page_context.get("timings"),
                        }
                        # Capture right-side numeric tokens that may be in the same row.
                        row_price, row_kcal = self._extract_row_values(col_lines, idx)
                        if row_price and not current.get("price"):
                            current["price"] = row_price
                        if row_kcal and not current.get("kcal"):
                            current["kcal"] = row_kcal
                        continue
                    # Join very short wrapped fragments to previous item name,
                    # but only when this line is not a new item candidate.
                    if current and text.isupper() and len(text) <= 12 and not line.get("icons"):
                        current["name"] = (current.get("name", "") + " " + text).strip()
                        continue
                    if current:
                        price = self._extract_price_from_line(line)
                        kcal = self._extract_kcal_from_line(line)
                        if price and not current.get("price"):
                            current["price"] = price
                            continue
                        if kcal and not current.get("kcal"):
                            current["kcal"] = kcal
                            # Merge allergen icons from kcal lines into the current item.
                            line_icons = [i for i in (line.get("icons") or []) if i not in ("veg", "non_veg")]
                            if line_icons:
                                merged = list({*(current.get("icons") or []), *line_icons})
                                current["icons"] = merged
                            continue
                        current["description"].append(text)
                if current:
                    items.append(current)
        for item in items:
            desc = item.get("description", [])
            if isinstance(desc, list):
                item["description"] = " ".join([d for d in desc if d]).strip()
            item["name"] = self._clean_item_name(item.get("name", ""))
        items = self._propagate_shared_prices(items)
        return items

    def _order_lines_for_seed(self, lines: List[Dict]) -> List[Dict]:
        if not lines:
            return []
        heights = []
        for line in lines:
            box = line.get("bbox", [0, 0, 0, 0])
            h = float(box[3] - box[1])
            if h > 0:
                heights.append(h)
        median_h = float(np.median(heights)) if heights else 24.0
        row_tol = max(6.0, median_h * 0.55)

        sorted_lines = sorted(lines, key=lambda l: (l.get("bbox", [0, 0, 0, 0])[1], l.get("bbox", [0, 0, 0, 0])[0]))
        rows: List[List[Dict]] = []
        row_centers: List[float] = []
        for line in sorted_lines:
            box = line.get("bbox", [0, 0, 0, 0])
            cy = (box[1] + box[3]) / 2.0
            if not rows or abs(cy - row_centers[-1]) > row_tol:
                rows.append([line])
                row_centers.append(cy)
            else:
                rows[-1].append(line)
                row_centers[-1] = (row_centers[-1] + cy) / 2.0

        ordered: List[Dict] = []
        for row in rows:
            row_sorted = sorted(row, key=lambda l: l.get("bbox", [0, 0, 0, 0])[0])
            ordered.extend(row_sorted)
        return ordered

    def _extract_row_values(self, ordered_lines: List[Dict], idx: int) -> Tuple[str | None, str | None]:
        if idx < 0 or idx >= len(ordered_lines):
            return None, None
        line = ordered_lines[idx]
        box = line.get("bbox", [0, 0, 0, 0])
        x0, y0, x1, y1 = box
        cy = (y0 + y1) / 2.0
        h = max(8.0, y1 - y0)
        price = None
        kcal = None
        start = max(0, idx - 2)
        end = min(len(ordered_lines), idx + 4)
        for j in range(start, end):
            if j == idx:
                continue
            other = ordered_lines[j]
            ob = other.get("bbox", [0, 0, 0, 0])
            oc = (ob[1] + ob[3]) / 2.0
            if abs(oc - cy) > (h * 1.2):
                continue
            if ob[0] <= x0:
                continue
            if kcal is None:
                kval = self._extract_kcal_from_line(other)
                if kval:
                    kcal = kval
            if price is None:
                pval = self._extract_price_from_line(other)
                if pval:
                    price = pval
        return price, kcal

    def _looks_like_legend_line(self, text: str) -> bool:
        lower = (text or "").lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", lower) if t]
        if not tokens:
            return False
        if any(ch.isdigit() for ch in lower):
            return False
        if len(tokens) > 4:
            return False
        # Legend lines are short and consist almost entirely of allergen/label words.
        legend_tokens = {
            "v",
            "vg",
            "gf",
            "df",
            "nf",
            "sf",
            "veg",
            "vegan",
            "vegetarian",
            "nonveg",
            "nonvegetarian",
            "non-veg",
            "gluten",
            "free",
            "dairy",
            "allergen",
            "allergens",
            "contains",
            "may",
            "trace",
            "signature",
            "dish",
        }
        legend_words = set(ALLOWED_LABELS) | legend_tokens
        hit = [t for t in tokens if t in legend_words]
        ratio = len(hit) / max(len(tokens), 1)
        if ratio < 0.8:
            return False
        if len(tokens) <= 2 and ratio == 1.0:
            return True
        if any(t in legend_tokens or len(t) <= 2 for t in tokens):
            return True
        return False

    def _looks_like_icon_noise_line(self, text: str) -> bool:
        if not text:
            return False
        t = re.sub(r"\s+", "", text)
        if not t:
            return False
        if re.fullmatch(r"[\W_]+", t):
            return True
        alnum = sum(1 for c in t if c.isalnum())
        if len(t) <= 3 and alnum <= 1:
            return True
        if len(t) <= 6 and (alnum / max(len(t), 1)) < 0.4:
            return True
        return False

    def _infer_footer_top(self, lines: List[Dict], page_h: int) -> int:
        default_top = int(page_h * (1.0 - self.config.footer_band_ratio))
        if not lines:
            return default_top
        heights = [abs(l.get("bbox", [0, 0, 0, 0])[3] - l.get("bbox", [0, 0, 0, 0])[1]) for l in lines]
        median_h = float(np.median([h for h in heights if h > 0])) if heights else 24.0
        pad = max(6.0, median_h * 1.2)

        candidates = []
        for line in lines:
            text = (line.get("text") or "").strip()
            if not text:
                continue
            if self._looks_like_legend_line(text) or self._is_footer_text(text):
                y0 = line.get("bbox", [0, 0, 0, 0])[1]
                candidates.append(y0)
        if not candidates:
            return default_top
        legend_top = int(min(candidates) - pad)
        legend_top = max(0, min(page_h, legend_top))
        return max(default_top, legend_top)

    def _clean_item_name(self, text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"^[^a-zA-Z0-9]+", "", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned

    def _is_name_line(self, line: Dict) -> bool:
        text = (line.get("name_text") or line.get("text") or "").strip()
        if not text:
            return False
        lower = text.lower()
        if self._is_generic_menu_header_text(lower):
            return False
        if "am to" in lower or "pm to" in lower:
            return False
        if any(tok in lower for tok in ("price", "kcal", "cal", "allergen", "veg", "non-veg", "vegetarian")):
            return False
        if line.get("name_text") and line.get("price_text"):
            return True
        if text.strip().startswith(("le]", "fe]", "4]")):
            return True
        if sum(c.isdigit() for c in text) / max(len(text), 1) > 0.2:
            return False
        if len(text) < 4:
            return False
        letters = [c for c in text if c.isalpha()]
        if letters:
            upper_ratio = sum(c.isupper() for c in letters) / max(len(letters), 1)
            if upper_ratio >= 0.7 and len(text) <= 60:
                return True
        return False

    def _extract_price_from_line(self, line: Dict) -> str | None:
        price_text = (line.get("price_text") or "").strip()
        if price_text:
            normalized = self._normalize_price(price_text)
            if normalized:
                return normalized
        text = (line.get("text") or "").strip()
        return self._normalize_price(text)

    def _extract_kcal_from_line(self, line: Dict) -> str | None:
        kcal_text = (line.get("kcal_text") or "").strip()
        if kcal_text:
            normalized = self._normalize_kcal(kcal_text)
            if normalized:
                return normalized
        text = (line.get("text") or "").strip()
        normalized = self._normalize_kcal(text)
        if normalized:
            return normalized
        return None

    def _normalize_price(self, text: str) -> str | None:
        if not text:
            return None
        lower = text.lower()
        if "kcal" in lower or "calorie" in lower or re.search(r"\bcal\b", lower):
            return None
        if re.search(r"\b\d{1,2}:\d{2}\b", lower) and ("am" in lower or "pm" in lower):
            return None
        decimal_matches = re.findall(r"(?:â‚¹|inr|rs|aed|sar|\$)?\s*(?:\d{1,6}[.,]\d{1,2}|[.,]\d{2})", text.lower())
        if decimal_matches:
            candidate = decimal_matches[-1].strip()
            return candidate or None
        matches = re.findall(r"(?:â‚¹|inr|rs|aed|sar|\$)?\s*\d{2,6}", text.lower())
        if not matches:
            return None
        candidate = matches[-1].strip()
        return candidate or None

    def _normalize_kcal(self, text: str) -> str | None:
        if not text:
            return None
        t = text.lower().replace("kcai", "kcal")
        if "kcal" not in t and "cal" not in t:
            return None
        matches = re.findall(r"\d{1,4}(?:[.,]\d{1,2})?", t)
        if not matches:
            return None
        return matches[0].replace(",", ".")

    def _parse_json_maybe(self, text: str) -> Dict | None:
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            return json.loads(cleaned)
        except Exception:
            return None

    def suggest_page_layout_with_openai_vision(
        self,
        page_image_png: bytes,
        page_no: int | None = None,
    ) -> Dict[str, Any] | None:
        if not self.config.vision_layout_enabled:
            return None
        if not page_image_png:
            return None

        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        model = os.getenv("OPENAI_VISION_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        try:
            client = OpenAI(api_key=api_key)
            image_b64 = base64.b64encode(page_image_png).decode("ascii")
            image_url = f"data:image/png;base64,{image_b64}"

            system_text = (
                "You are a strict document layout analyzer for menu pages. "
                "Return JSON only. "
                "Detect main content columns/blocks that contain dish text with prices/descriptions. "
                "Ignore decorative areas and legend/footer zones. "
                "Do not output narrow price-only strips as separate regions."
            )
            user_text = (
                "Analyze this page image and output JSON with keys: "
                "page, is_multi_column, confidence, regions. "
                "Use regions as an array of objects: "
                "{reading_order:int, kind:string, confidence:number, bbox_norm:[x0,y0,x1,y1]}. "
                "bbox_norm must be normalized 0..1. "
                "If single-column, still output one region for main menu content."
            )
            if page_no is not None:
                user_text += f" Page number hint: {int(page_no)}."

            response = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": system_text}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": user_text},
                            {"type": "input_image", "image_url": image_url},
                        ],
                    },
                ],
                timeout=max(int(self.config.openai_timeout), 90),
                temperature=0.0,
            )
        except Exception:
            return None

        raw_text = ""
        try:
            raw_text = response.output[0].content[0].text
        except Exception:
            try:
                raw_text = str(getattr(response, "output_text", "") or "")
            except Exception:
                raw_text = ""
        parsed = self._parse_json_maybe(raw_text)
        if not isinstance(parsed, dict):
            return None

        def _f(v: Any, default: float = 0.0) -> float:
            try:
                return float(v)
            except Exception:
                return default

        is_multi = bool(parsed.get("is_multi_column"))
        confidence = _f(parsed.get("confidence"), 0.0)
        regions_in = parsed.get("regions")
        if not isinstance(regions_in, list):
            regions_in = []

        out_regions: List[Dict[str, Any]] = []
        for idx, reg in enumerate(regions_in):
            if not isinstance(reg, dict):
                continue
            bbox = reg.get("bbox_norm")
            if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            x0 = max(0.0, min(1.0, _f(bbox[0], 0.0)))
            y0 = max(0.0, min(1.0, _f(bbox[1], 0.0)))
            x1 = max(0.0, min(1.0, _f(bbox[2], 0.0)))
            y1 = max(0.0, min(1.0, _f(bbox[3], 0.0)))
            if x1 <= x0 or y1 <= y0:
                continue
            if (x1 - x0) < 0.08 or (y1 - y0) < 0.12:
                continue
            order = int(reg.get("reading_order")) if isinstance(reg.get("reading_order"), (int, float)) else (idx + 1)
            kind = str(reg.get("kind") or "column").strip().lower() or "column"
            r_conf = _f(reg.get("confidence"), confidence)
            out_regions.append(
                {
                    "reading_order": order,
                    "kind": kind,
                    "confidence": r_conf,
                    "bbox_norm": [round(x0, 5), round(y0, 5), round(x1, 5), round(y1, 5)],
                }
            )

        if not out_regions:
            return None
        out_regions.sort(key=lambda r: (int(r.get("reading_order") or 0), float((r.get("bbox_norm") or [0])[0])))
        if len(out_regions) > int(self.config.vision_layout_max_regions):
            out_regions = out_regions[: int(self.config.vision_layout_max_regions)]

        if confidence < float(self.config.vision_layout_min_confidence) and len(out_regions) >= 2:
            # Low-confidence region splits should be ignored to avoid regressions.
            return None

        return {
            "page": int(page_no) if page_no is not None else None,
            "is_multi_column": is_multi,
            "confidence": round(confidence, 4),
            "regions": out_regions,
        }

    def _docai_get_document(self, docai: Dict) -> Dict:
        if "document" in docai and isinstance(docai["document"], dict):
            return docai["document"]
        return docai

    def _docai_extract_lines(
        self,
        docai_doc: Dict,
        docai_pages: List[Dict],
        page_index: int,
        image_shape: Tuple[int, int, int],
    ) -> List[Dict]:
        if page_index >= len(docai_pages):
            return []
        page = docai_pages[page_index]
        doc_text = docai_doc.get("text", "")
        height, width = image_shape[0], image_shape[1]

        dim = {}
        if isinstance(page, dict):
            dim = page.get("dimension", {}) or page.get("page_dimension", {}) or {}
        doc_w = (
            dim.get("width")
            or (page.get("width") if isinstance(page, dict) else None)
            or width
        )
        doc_h = (
            dim.get("height")
            or (page.get("height") if isinstance(page, dict) else None)
            or height
        )
        scale_x = width / float(doc_w) if doc_w else 1.0
        scale_y = height / float(doc_h) if doc_h else 1.0

        def extract_from_items(items: List[Dict]) -> List[Dict]:
            lines_out: List[Dict] = []
            for item in items:
                if not isinstance(item, dict):
                    continue

                # Layout-parser normalized pages may already provide text+bbox directly.
                raw_text = str(item.get("text") or "").strip()
                raw_bbox = item.get("bbox")
                if raw_text and isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
                    try:
                        x0, y0, x1, y1 = [float(v) for v in raw_bbox]
                    except Exception:
                        x0 = y0 = x1 = y1 = 0.0

                    bbox_norm = item.get("bbox_normalized")
                    if isinstance(bbox_norm, list) and bbox_norm and isinstance(bbox_norm[0], dict):
                        xs = [float(v.get("x", 0.0)) * width for v in bbox_norm]
                        ys = [float(v.get("y", 0.0)) * height for v in bbox_norm]
                        if xs and ys:
                            x0, x1 = min(xs), max(xs)
                            y0, y1 = min(ys), max(ys)
                    else:
                        x0 *= scale_x
                        y0 *= scale_y
                        x1 *= scale_x
                        y1 *= scale_y

                    if x1 > x0 and y1 > y0:
                        lines_out.append({"text": raw_text, "bbox": [x0, y0, x1, y1], "icons": []})
                    continue

                layout = item.get("layout", {})
                text = self._docai_text_from_anchor(doc_text, layout)
                if not text:
                    continue
                bbox = self._docai_layout_bbox(layout, width, height, scale_x, scale_y)
                if bbox is None:
                    continue
                lines_out.append({"text": text, "bbox": bbox, "icons": []})
            return lines_out

        lines = page.get("lines", []) if isinstance(page, dict) else []
        ocr_lines = page.get("ocr_lines", []) if isinstance(page, dict) else []
        if ocr_lines:
            extracted = extract_from_items(ocr_lines)
            if extracted:
                return extracted
        if lines:
            return extract_from_items(lines)

        # Fallback to paragraphs if lines are missing
        paragraphs = page.get("paragraphs", []) if isinstance(page, dict) else []
        if paragraphs:
            return extract_from_items(paragraphs)

        return []

    def _docai_extract_blocks(
        self,
        docai_doc: Dict,
        docai_pages: List[Dict],
        page_index: int,
        image_shape: Tuple[int, int, int],
    ) -> List[Dict]:
        if page_index >= len(docai_pages):
            return []
        page = docai_pages[page_index]
        doc_text = docai_doc.get("text", "")
        height, width = image_shape[0], image_shape[1]

        dim = {}
        if isinstance(page, dict):
            dim = page.get("dimension", {}) or page.get("page_dimension", {}) or {}
        doc_w = (
            dim.get("width")
            or (page.get("width") if isinstance(page, dict) else None)
            or width
        )
        doc_h = (
            dim.get("height")
            or (page.get("height") if isinstance(page, dict) else None)
            or height
        )
        scale_x = width / float(doc_w) if doc_w else 1.0
        scale_y = height / float(doc_h) if doc_h else 1.0

        blocks = page.get("blocks", []) if isinstance(page, dict) else []
        if not blocks:
            paragraphs = page.get("paragraphs", []) if isinstance(page, dict) else []
            blocks = paragraphs

        out: List[Dict] = []
        for item in blocks:
            if not isinstance(item, dict):
                continue

            raw_text = str(item.get("text") or "").strip()
            raw_bbox = item.get("bbox")
            if raw_text and isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
                try:
                    x0, y0, x1, y1 = [float(v) for v in raw_bbox]
                except Exception:
                    x0 = y0 = x1 = y1 = 0.0

                bbox_norm = item.get("bbox_normalized")
                if isinstance(bbox_norm, list) and bbox_norm and isinstance(bbox_norm[0], dict):
                    xs = [float(v.get("x", 0.0)) * width for v in bbox_norm]
                    ys = [float(v.get("y", 0.0)) * height for v in bbox_norm]
                    if xs and ys:
                        x0, x1 = min(xs), max(xs)
                        y0, y1 = min(ys), max(ys)
                else:
                    x0 *= scale_x
                    y0 *= scale_y
                    x1 *= scale_x
                    y1 *= scale_y

                if x1 > x0 and y1 > y0:
                    out.append({"text": raw_text, "bbox": [x0, y0, x1, y1]})
                continue

            layout = item.get("layout", {})
            text = self._docai_text_from_anchor(doc_text, layout)
            if not text:
                continue
            bbox = self._docai_layout_bbox(layout, width, height, scale_x, scale_y)
            if bbox is None:
                continue
            out.append({"text": text, "bbox": bbox})
        return out

    def _docai_text_from_anchor(self, doc_text: str, layout: Dict) -> str:
        anchor = layout.get("textAnchor", {}) if isinstance(layout, dict) else {}
        segments = anchor.get("textSegments", [])
        if not segments:
            # Some exports use "textSegments" under "text_anchor"
            anchor = layout.get("text_anchor", {}) if isinstance(layout, dict) else {}
            segments = anchor.get("text_segments", []) or anchor.get("textSegments", [])
        if not segments:
            return ""
        parts = []
        for seg in segments:
            start = seg.get("startIndex", seg.get("start_index", 0))
            end = seg.get("endIndex", seg.get("end_index", 0))
            try:
                s = int(start)
            except Exception:
                s = 0
            try:
                e = int(end)
            except Exception:
                e = s
            if e > s and s < len(doc_text):
                parts.append(doc_text[s:e])
        return "".join(parts).strip()

    def _docai_layout_bbox(
        self,
        layout: Dict,
        image_w: int,
        image_h: int,
        scale_x: float,
        scale_y: float,
    ) -> List[float] | None:
        poly = layout.get("boundingPoly", {}) if isinstance(layout, dict) else {}
        vertices = poly.get("vertices")
        norm_vertices = poly.get("normalizedVertices")
        if norm_vertices:
            xs = [float(v.get("x", 0.0)) * image_w for v in norm_vertices]
            ys = [float(v.get("y", 0.0)) * image_h for v in norm_vertices]
        elif vertices:
            xs = [float(v.get("x", 0.0)) * scale_x for v in vertices]
            ys = [float(v.get("y", 0.0)) * scale_y for v in vertices]
        else:
            return None
        if not xs or not ys:
            return None
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 <= x0 or y1 <= y0:
            return None
        return [float(x0), float(y0), float(x1), float(y1)]


    def _is_footer_text(self, text: str) -> bool:
        t = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not t:
            return False
        if any(
            key in t
            for key in (
                "kindly inform",
                "all prices are",
                "exclusive of taxes",
                "service charge",
                "calorie content",
                "before you order",
                "some dishes can be made",
                "please let us know",
                "please let us any",
            )
        ):
            return True
        footer_patterns = (
            r"\ballerg(?:y|ies)\b",
            r"\bgluten\s*free\b",
            r"\btaxes?\b.*\b(charged|extra|excluded|exclusive)\b",
            r"\bcharged\s+extra\b",
            r"\bsubject\s+to\b.*\btax\b",
            r"\bprices?\b.*\bexclusive\b",
        )
        for pat in footer_patterns:
            if re.search(pat, t):
                return True
        return False

    def _looks_like_legend_line(self, text: str) -> bool:
        lower = (text or "").lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", lower) if t]
        if not tokens:
            return False
        if any(ch.isdigit() for ch in lower):
            return False
        if "kcal" in lower:
            return False
        if len(tokens) > 4:
            return False
        # Legend lines are short and consist almost entirely of allergen/label words.
        legend_tokens = {
            "v",
            "vg",
            "gf",
            "df",
            "nf",
            "sf",
            "veg",
            "vegan",
            "vegetarian",
            "nonveg",
            "nonvegetarian",
            "non-veg",
            "gluten",
            "free",
            "dairy",
            "allergen",
            "allergens",
            "contains",
            "may",
            "trace",
            "signature",
            "dish",
        }
        legend_words = set(ALLOWED_LABELS) | legend_tokens
        hit = [t for t in tokens if t in legend_words]
        ratio = len(hit) / max(len(tokens), 1)
        if ratio < 0.8:
            return False
        if len(tokens) <= 2 and ratio == 1.0:
            return True
        if any(t in legend_tokens or len(t) <= 2 for t in tokens):
            return True
        return False

    def _looks_like_icon_noise_line(self, text: str) -> bool:
        if not text:
            return False
        t = re.sub(r"\s+", "", text)
        if not t:
            return False
        if re.fullmatch(r"[\W_]+", t):
            return True
        alnum = sum(1 for c in t if c.isalnum())
        if len(t) <= 3 and alnum <= 1:
            return True
        if len(t) <= 6 and (alnum / max(len(t), 1)) < 0.4:
            return True
        return False

    def _infer_footer_top(self, lines: List[Dict], page_h: int) -> int:
        default_top = int(page_h * (1.0 - self.config.footer_band_ratio))
        if not lines:
            return default_top
        heights = [abs(l.get("bbox", [0, 0, 0, 0])[3] - l.get("bbox", [0, 0, 0, 0])[1]) for l in lines]
        median_h = float(np.median([h for h in heights if h > 0])) if heights else 24.0
        pad = max(6.0, median_h * 1.2)

        candidates = []
        for line in lines:
            text = (line.get("text") or "").strip()
            if not text:
                continue
            if self._looks_like_legend_line(text) or self._is_footer_text(text):
                y0 = line.get("bbox", [0, 0, 0, 0])[1]
                candidates.append(y0)
        if not candidates:
            return default_top
        legend_top = int(min(candidates) - pad)
        legend_top = max(0, min(page_h, legend_top))
        return max(default_top, legend_top)

