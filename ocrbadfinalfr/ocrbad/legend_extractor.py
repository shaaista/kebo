from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    cv2 = None

try:
    from PIL import Image
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover - required dependency
    raise RuntimeError("PyMuPDF (fitz) is required. pip install pymupdf") from exc

try:
    from .config import ALLOWED_LABELS, PHRASE_SYNONYMS, TOKEN_SYNONYMS, LegendConfig  # type: ignore
except ImportError:  # fallback when run as scripts inside ocr/
    from config import ALLOWED_LABELS, PHRASE_SYNONYMS, TOKEN_SYNONYMS, LegendConfig  # type: ignore

Word = Tuple[float, float, float, float, str]
Box = Tuple[float, float, float, float]


@dataclass
class LabelHit:
    label: str
    bbox: Box
    order: int


@dataclass
class IconEntry:
    path: Path
    score: float


class LegendExtractor:
    def __init__(self, config: LegendConfig | None = None) -> None:
        self.config = config or LegendConfig()
        self._init_ocr()

    def _init_ocr(self) -> None:
        if not self.config.use_ocr_fallback:
            self._pytesseract = None
            return
        try:
            import pytesseract  # type: ignore

            if self.config.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = self.config.tesseract_cmd
            self._pytesseract = pytesseract
        except Exception:
            self._pytesseract = None

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> List[Dict]:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        icons_dir = output_dir / "icons"
        icons_dir.mkdir(parents=True, exist_ok=True)

        results: List[Dict] = []
        icon_bank: Dict[str, IconEntry] = {}
        bank_state: Dict[str, int] = {"best_label_count": -1}
        doc = fitz.open(pdf_path)

        for page_index in range(len(doc)):
            page = doc[page_index]
            image_rgb, sx, sy = self._render_page(page)
            page_result = self.extract_page_legend(
                page=page,
                image_rgb=image_rgb,
                page_index=page_index,
                scale_x=sx,
                scale_y=sy,
                icons_dir=icons_dir,
                icon_bank=icon_bank,
                bank_state=bank_state,
            )
            results.append(page_result)

            out_json = output_dir / f"page_{page_index + 1:02d}.json"
            with out_json.open("w", encoding="utf-8") as f:
                json.dump(page_result, f, indent=2)

        return results

    def extract_page_legend(
        self,
        page,
        image_rgb: np.ndarray,
        page_index: int,
        scale_x: float,
        scale_y: float,
        icons_dir: Path,
        icon_bank: Dict[str, IconEntry],
        bank_state: Dict[str, int],
    ) -> Dict:
        footer_top_pdf = page.rect.height * (1.0 - self.config.footer_band_ratio)
        footer_top_img = int(image_rgb.shape[0] * (1.0 - self.config.footer_band_ratio))

        words = self._extract_footer_words_pdf(page, footer_top_pdf)
        words_space = "pdf"

        if not words and self._pytesseract is not None:
            words = self._extract_footer_words_ocr(image_rgb, footer_top_img)
            words_space = "img"

        label_hits = self._detect_labels(words)
        label_hits = self._filter_label_hits_to_legend_rows(label_hits)

        label_boxes_img: Dict[str, Box] = {}
        label_first_seen: Dict[str, int] = {}

        for hit in label_hits:
            if hit.label not in ALLOWED_LABELS:
                continue
            bbox = hit.bbox
            if words_space == "pdf":
                bbox = self._scale_box(bbox, scale_x, scale_y)
            if hit.label not in label_boxes_img:
                label_boxes_img[hit.label] = bbox
                label_first_seen[hit.label] = hit.order
            else:
                # Prefer the larger label box (usually clearer in legend)
                prev = label_boxes_img[hit.label]
                if (bbox[3] - bbox[1]) > (prev[3] - prev[1]):
                    label_boxes_img[hit.label] = bbox

        labels_sorted = sorted(label_boxes_img.keys(), key=lambda l: label_first_seen.get(l, 0))
        legend_found = len(labels_sorted) > 0

        icon_map: Dict[str, str] = {}
        icon_locations: Dict[str, Box] = {}

        preferred_side = None
        median_label_h = None
        if legend_found:
            preferred_side = self._infer_legend_side(image_rgb, label_boxes_img)
            heights = [abs(b[3] - b[1]) for b in label_boxes_img.values()]
            if heights:
                median_label_h = float(np.median(heights))

        update_bank = False
        if legend_found:
            label_count = len(labels_sorted)
            if label_count > bank_state.get("best_label_count", -1):
                bank_state["best_label_count"] = label_count
                update_bank = True
                icon_bank.clear()

        components: List[Box] = []
        used_components: set[int] = set()
        comp_candidates: Dict[str, Tuple[np.ndarray, Box, float, int, int]] = {}
        median_icon_w = None
        median_icon_h = None
        if legend_found and median_label_h is not None:
            components = self._find_footer_icon_components(image_rgb, footer_top_img, median_label_h)
            sizes: List[Tuple[int, int]] = []
            for label in labels_sorted:
                comp_idx = self._pick_component_for_label(
                    label_boxes_img[label], components, preferred_side, used_components, median_label_h
                )
                if comp_idx is None:
                    continue
                crop, crop_box = self._crop_from_component(image_rgb, components[comp_idx], median_label_h)
                if crop is None or crop_box is None or not self._crop_valid(crop):
                    continue
                h, w = crop.shape[:2]
                sizes.append((w, h))
                comp_candidates[label] = (crop, crop_box, self._score_crop(crop), w, h)
                used_components.add(comp_idx)
            if sizes:
                median_icon_w = int(np.median([s[0] for s in sizes]))
                median_icon_h = int(np.median([s[1] for s in sizes]))

        used_boxes: List[Box] = []
        for label in labels_sorted:
            if label in icon_bank and not update_bank:
                icon_map[label] = str(icon_bank[label].path.relative_to(icons_dir.parent))
                continue

            bbox = label_boxes_img[label]
            crop = None
            crop_box = None
            score = 0.0

            # Component-based assignment (global, avoids duplicates)
            if label in comp_candidates:
                crop, crop_box, score, w, h = comp_candidates[label]

            if crop is None:
                candidates: List[Tuple[np.ndarray, Box, float]] = []
                side_order = ("left", "right")
                if preferred_side in ("left", "right"):
                    side_order = (preferred_side, "right" if preferred_side == "left" else "left")

                for side in side_order:
                    crop, crop_box, score = self._crop_icon_from_label_window(image_rgb, bbox, side=side)
                    if crop is None or crop_box is None:
                        continue
                    if not self._crop_valid(crop):
                        continue
                    if self._overlaps_used(crop_box, used_boxes):
                        continue
                    candidates.append((crop, crop_box, score))

                if not candidates and preferred_side in ("left", "right"):
                    # fallback: allow overlap on the opposite side if nothing was found
                    other = "right" if preferred_side == "left" else "left"
                    crop, crop_box, score = self._crop_icon_from_label_window(image_rgb, bbox, side=other)
                    if crop is not None and crop_box is not None and self._crop_valid(crop):
                        candidates.append((crop, crop_box, score))

                if candidates:
                    crop, crop_box, score = max(candidates, key=lambda c: c[2])
                else:
                    crop, crop_box, score = None, None, 0.0
            else:
                # If component crop looks too small or clipped, try the label-window fallback
                if median_icon_w and median_icon_h and crop is not None:
                    h, w = crop.shape[:2]
                    too_small = (
                        w < (median_icon_w * self.config.icon_outlier_min_ratio)
                        or h < (median_icon_h * self.config.icon_outlier_min_ratio)
                    )
                    edge_touch = self._ink_touches_edges(crop)
                    if too_small or edge_touch:
                        fallback, fb_box, fb_score = self._crop_icon_from_label_window(
                            image_rgb, bbox, side=preferred_side or "left"
                        )
                        if (
                            fallback is not None
                            and fb_box is not None
                            and self._crop_valid(fallback)
                            and (not self._ink_touches_edges(fallback) or fb_score >= score)
                        ):
                            crop, crop_box, score = fallback, fb_box, fb_score
            if crop is not None and self._crop_valid(crop):
                if crop_box is not None and self._ink_touches_edges(crop):
                    pad = int(max(2, min(crop.shape[:2]) * self.config.icon_post_pad_ratio))
                    crop, crop_box = self._recrop_with_pad_until_clear(image_rgb, crop_box, pad)
                if self._ink_touches_edges(crop):
                    pad = int(max(2, min(crop.shape[:2]) * (self.config.icon_post_pad_ratio * 0.8)))
                    crop = self._pad_crop_white(crop, pad)
                icon_path = icons_dir / f"{label}.png"
                # Save with high quality (using PIL if possible, or cv2)
                if cv2 is not None:
                    # cv2 default is compression 3, let's use 0 (larger file, perfect quality)
                    cv2.imwrite(str(icon_path), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_PNG_COMPRESSION, 0])
                else:
                    # Fallback to PIL if no cv2 (though cv2 is likely)
                    Image.fromarray(crop).save(icon_path, format="PNG", compress_level=0)
                
                icon_bank[label] = IconEntry(path=icon_path, score=score)
                if crop_box is not None:
                    icon_locations[label] = crop_box
                    used_boxes.append(crop_box)

            entry = icon_bank.get(label)
            if entry is not None:
                icon_map[label] = str(entry.path.relative_to(icons_dir.parent))

        result = {
            "page": page_index + 1,
            "legend_found": legend_found,
            "labels": labels_sorted,
            "icon_map": icon_map,
        }

        if self.config.debug:
            result["icon_locations"] = icon_locations
            if preferred_side:
                result["legend_side"] = preferred_side

        return result

    def _infer_legend_side(self, image_rgb: np.ndarray, label_boxes_img: Dict[str, Box]) -> str | None:
        if not label_boxes_img:
            return None
        wins = {"left": 0, "right": 0}
        scores = {"left": 0.0, "right": 0.0}
        for _, bbox in label_boxes_img.items():
            left_crop, left_box, left_score = self._crop_icon_from_label_window(image_rgb, bbox, side="left")
            right_crop, right_box, right_score = self._crop_icon_from_label_window(image_rgb, bbox, side="right")
            if left_crop is not None and left_box is not None and self._crop_valid(left_crop):
                scores["left"] += left_score
            if right_crop is not None and right_box is not None and self._crop_valid(right_crop):
                scores["right"] += right_score
            if left_score > right_score * self.config.legend_side_score_ratio:
                wins["left"] += 1
            elif right_score > left_score * self.config.legend_side_score_ratio:
                wins["right"] += 1
        if wins["left"] >= self.config.legend_side_min_votes and wins["left"] > wins["right"]:
            return "left"
        if wins["right"] >= self.config.legend_side_min_votes and wins["right"] > wins["left"]:
            return "right"
        # fallback to total scores if wins are tied
        if scores["left"] > scores["right"] * self.config.legend_side_score_ratio:
            return "left"
        if scores["right"] > scores["left"] * self.config.legend_side_score_ratio:
            return "right"
        # final fallback: use ink density on each side of label boxes
        left_ink, right_ink = self._legend_side_ink_density(image_rgb, label_boxes_img)
        if left_ink > right_ink * self.config.legend_side_score_ratio:
            return "left"
        if right_ink > left_ink * self.config.legend_side_score_ratio:
            return "right"
        return None

    def _legend_side_ink_density(
        self, image_rgb: np.ndarray, label_boxes_img: Dict[str, Box]
    ) -> Tuple[float, float]:
        if cv2 is None:
            return 0.0, 0.0
        gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
        left_ink = 0.0
        right_ink = 0.0
        for bbox in label_boxes_img.values():
            x0, y0, x1, y1 = bbox
            lh = max(1.0, y1 - y0)
            pad = int(lh * 0.3)
            y0i = max(0, int(y0 - pad))
            y1i = min(gray.shape[0], int(y1 + pad))
            # sample windows on each side
            lw = int(lh * 3.0)
            lx1 = max(0, int(x0 - 5))
            lx0 = max(0, lx1 - lw)
            rx0 = min(gray.shape[1], int(x1 + 5))
            rx1 = min(gray.shape[1], rx0 + lw)
            if lx1 > lx0:
                left_ink += float((gray[y0i:y1i, lx0:lx1] < 245).sum())
            if rx1 > rx0:
                right_ink += float((gray[y0i:y1i, rx0:rx1] < 245).sum())
        return left_ink, right_ink

    def _overlaps_used(self, bbox: Box, used_boxes: List[Box]) -> bool:
        for other in used_boxes:
            if self._iou(bbox, other) >= self.config.icon_iou_dedupe_threshold:
                return True
        return False

    def _iou(self, a: Box, b: Box) -> float:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        inter_x0 = max(ax0, bx0)
        inter_y0 = max(ay0, by0)
        inter_x1 = min(ax1, bx1)
        inter_y1 = min(ay1, by1)
        inter_w = max(0.0, inter_x1 - inter_x0)
        inter_h = max(0.0, inter_y1 - inter_y0)
        inter = inter_w * inter_h
        if inter <= 0.0:
            return 0.0
        area_a = max(0.0, (ax1 - ax0)) * max(0.0, (ay1 - ay0))
        area_b = max(0.0, (bx1 - bx0)) * max(0.0, (by1 - by0))
        return inter / (area_a + area_b - inter + 1e-6)

    def _render_page(self, page) -> Tuple[np.ndarray, float, float]:
        pix = page.get_pixmap(dpi=self.config.dpi, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        scale_x = pix.width / page.rect.width
        scale_y = pix.height / page.rect.height
        return img, scale_x, scale_y

    def _extract_footer_words_pdf(self, page, footer_top_pdf: float) -> List[Word]:
        words = []
        for x0, y0, x1, y1, text, *_ in page.get_text("words"):
            if not text.strip():
                continue
            y_center = (y0 + y1) / 2.0
            if y_center >= footer_top_pdf:
                words.append((x0, y0, x1, y1, text))
        return words

    def _extract_footer_words_ocr(self, image_rgb: np.ndarray, footer_top_img: int) -> List[Word]:
        if self._pytesseract is None:
            return []
        footer_img = image_rgb[footer_top_img:, :]
        try:
            data = self._pytesseract.image_to_data(footer_img, output_type=self._pytesseract.Output.DICT)
        except Exception:
            return []
        words: List[Word] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            x = float(data["left"][i])
            y = float(data["top"][i]) + footer_top_img
            w = float(data["width"][i])
            h = float(data["height"][i])
            words.append((x, y, x + w, y + h, text))
        return words

    def _detect_labels(self, words: Sequence[Word]) -> List[LabelHit]:
        if not words:
            return []

        line_tol = self._line_tolerance(words)
        lines = self._group_words_into_lines(words, line_tol)

        hits: List[LabelHit] = []
        order = 0

        for line in lines:
            tokens = [self._normalize_token(w[4]) for w in line]
            used = [False] * len(tokens)

            # Multi-token phrases (length 2 and 3)
            for length in (3, 2):
                for i in range(0, len(tokens) - length + 1):
                    if any(used[i : i + length]):
                        continue
                    phrase = tuple(tokens[i : i + length])
                    if phrase in PHRASE_SYNONYMS:
                        label = PHRASE_SYNONYMS[phrase]
                        bbox = self._union_boxes([line[j] for j in range(i, i + length)])
                        hits.append(LabelHit(label=label, bbox=bbox, order=order))
                        order += 1
                        for j in range(i, i + length):
                            used[j] = True

            # Single-token labels
            for i, token in enumerate(tokens):
                if used[i]:
                    continue
                label = self._map_token(token)
                if label:
                    bbox = (line[i][0], line[i][1], line[i][2], line[i][3])
                    hits.append(LabelHit(label=label, bbox=bbox, order=order))
                    order += 1

        # Only keep labels we care about
        hits = [h for h in hits if h.label in ALLOWED_LABELS]
        return hits

    def _filter_label_hits_to_legend_rows(self, hits: List[LabelHit]) -> List[LabelHit]:
        if not hits:
            return hits
        heights = [abs(h.bbox[3] - h.bbox[1]) for h in hits]
        if not heights:
            return hits
        median_h = float(np.median(heights))
        tol = max(2.0, median_h * 1.2)
        sorted_hits = sorted(hits, key=lambda h: ((h.bbox[1] + h.bbox[3]) / 2.0, h.bbox[0]))
        rows: List[List[LabelHit]] = []
        row_centers: List[float] = []
        for h in sorted_hits:
            yc = (h.bbox[1] + h.bbox[3]) / 2.0
            if not rows or abs(yc - row_centers[-1]) > tol:
                rows.append([h])
                row_centers.append(yc)
            else:
                rows[-1].append(h)
                row_centers[-1] = (row_centers[-1] + yc) / 2.0
        # choose up to two rows with most labels, but require legend-like density
        scored = sorted(rows, key=lambda r: len(r), reverse=True)
        selected = [r for r in scored if len(r) >= 2][:2]
        filtered = [h for row in selected for h in row]
        unique_labels = {h.label for h in filtered}
        if len(unique_labels) < max(3, self.config.ocr_fallback_min_labels):
            return []
        return filtered

    def _group_words_into_lines(self, words: Sequence[Word], line_tol: float) -> List[List[Word]]:
        sorted_words = sorted(words, key=lambda w: (w[1], w[0]))
        lines: List[List[Word]] = []
        line_y: List[float] = []

        for word in sorted_words:
            y_center = (word[1] + word[3]) / 2.0
            if not lines or abs(y_center - line_y[-1]) > line_tol:
                lines.append([word])
                line_y.append(y_center)
            else:
                lines[-1].append(word)
                # update line center slightly
                line_y[-1] = (line_y[-1] + y_center) / 2.0

        # sort each line by x
        for line in lines:
            line.sort(key=lambda w: w[0])
        return lines

    def _line_tolerance(self, words: Sequence[Word]) -> float:
        # Estimate a reasonable tolerance from word heights
        heights = [abs(w[3] - w[1]) for w in words]
        if not heights:
            return 2.0
        median = float(np.median(heights))
        page_height = max(w[3] for w in words)
        # clamp to a fraction of page height to avoid merging adjacent rows at high DPI
        return max(2.0, min(median * 0.8, page_height * self.config.line_merge_tol))

    def _normalize_token(self, token: str) -> str:
        token = token.lower().strip()
        token = re.sub(r"[^a-z0-9]+", "", token)
        return token

    def _map_token(self, token: str) -> str | None:
        if not token:
            return None
        if token in TOKEN_SYNONYMS:
            return TOKEN_SYNONYMS[token]
        if token.endswith("s") and token[:-1] in TOKEN_SYNONYMS:
            return TOKEN_SYNONYMS[token[:-1]]
        return None

    def _union_boxes(self, words: Sequence[Word]) -> Box:
        x0 = min(w[0] for w in words)
        y0 = min(w[1] for w in words)
        x1 = max(w[2] for w in words)
        y1 = max(w[3] for w in words)
        return (x0, y0, x1, y1)

    def _scale_box(self, bbox: Box, sx: float, sy: float) -> Box:
        return (bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy)

    def _crop_icon_best_side(
        self, image_rgb: np.ndarray, label_box: Box
    ) -> Tuple[np.ndarray | None, Box | None, float]:
        candidates: List[Tuple[np.ndarray | None, Box | None, float]] = []
        for side in ("left", "right"):
            crop, crop_box, score = self._find_icon_in_search_window(image_rgb, label_box, side)
            if crop is None:
                crop, crop_box = self._crop_icon(image_rgb, label_box, side)
                score = self._score_crop(crop)
            candidates.append((crop, crop_box, score))

        # pick highest score
        candidates.sort(key=lambda c: c[2], reverse=True)
        crop, crop_box, score = candidates[0]
        if crop is not None and score > 0:
            if self._crop_valid(crop):
                return crop, crop_box, score

        # Retry with expanded padding if both failed
        crop, crop_box = self._crop_icon(image_rgb, label_box, "left", scale_mult=1.3, pad_mult=1.5)
        if self._crop_valid(crop):
            return crop, crop_box, self._score_crop(crop)
        crop, crop_box = self._crop_icon(image_rgb, label_box, "right", scale_mult=1.3, pad_mult=1.5)
        if self._crop_valid(crop):
            return crop, crop_box, self._score_crop(crop)

        return None, None, 0.0

    def _crop_icon_from_label_window(
        self, image_rgb: np.ndarray, label_box: Box, side: str = "left"
    ) -> Tuple[np.ndarray | None, Box | None, float]:
        if cv2 is None:
            return None, None, 0.0

        h, w, _ = image_rgb.shape
        x0, y0, x1, y1 = label_box
        label_h = max(1.0, y1 - y0)
        label_cy = (y0 + y1) / 2.0

        search_w = label_h * self.config.icon_search_scale_w
        search_h = label_h * self.config.icon_search_scale_h
        pad = self.config.padding_px

        y0s = label_cy - search_h / 2.0
        y1s = label_cy + search_h / 2.0

        if side == "left":
            x1s = x0 - pad
            x0s = x1s - search_w
        else:
            x0s = x1 + pad
            x1s = x0s + search_w

        x0s = int(max(0, x0s))
        y0s = int(max(0, y0s))
        x1s = int(min(w, x1s))
        y1s = int(min(h, y1s))

        if x1s <= x0s or y1s <= y0s:
            return None, None, 0.0

        search_area = float((y1s - y0s) * (x1s - x0s))
        search = image_rgb[y0s:y1s, x0s:x1s]
        gray = self._to_gray(search)
        try:
            _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        except Exception:
            return None, None, 0.0

        kernel = np.ones((3, 3), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel, iterations=2)
        bw = cv2.dilate(bw, kernel, iterations=1)

        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        if num_labels <= 1:
            return None, None, 0.0

        # choose component whose right edge is closest to label (left side),
        # or left edge closest (right side)
        best = None
        best_key = None
        for i in range(1, num_labels):
            x, y, w_c, h_c, area = stats[i]
            if area < max(6.0, label_h * 0.12):
                continue
            if search_area > 0.0:
                area_ratio = float(area) / search_area
                if area_ratio < self.config.component_min_area_ratio:
                    continue
                if area_ratio > self.config.component_max_area_ratio:
                    continue
            if h_c > 0:
                aspect = float(w_c) / float(h_c)
                if aspect < self.config.component_aspect_min:
                    continue
                if aspect > self.config.component_aspect_max:
                    continue
                size_ratio = float(h_c) / label_h
                if size_ratio < self.config.icon_size_min_ratio:
                    continue
                if size_ratio > self.config.icon_size_max_ratio:
                    continue
            comp_cy = y0s + y + (h_c / 2.0)
            if abs(comp_cy - label_cy) > label_h * 1.2:
                continue
            if side == "left":
                comp_right = x0s + x + w_c
                dx = x0 - comp_right
                if dx < 0 or dx > label_h * 6.0:
                    continue
                key = (dx, -area)
            else:
                comp_left = x0s + x
                dx = comp_left - x1
                if dx < 0 or dx > label_h * 6.0:
                    continue
                key = (dx, -area)
            if best_key is None or key < best_key:
                best_key = key
                best = (x, y, w_c, h_c)

        if best is None:
            return None, None, 0.0

        bx, by, bw_c, bh_c = best
        # Merge nearby components to capture full icon
        merge_pad = max(2, int(label_h * self.config.icon_merge_pad_ratio))
        ux0, uy0, ux1, uy1 = bx, by, bx + bw_c, by + bh_c
        changed = True
        while changed:
            changed = False
            for i in range(1, num_labels):
                x, y, w_c, h_c, area = stats[i]
                if area < max(6.0, label_h * 0.12):
                    continue
                if search_area > 0.0:
                    area_ratio = float(area) / search_area
                    if area_ratio < self.config.component_min_area_ratio:
                        continue
                    if area_ratio > self.config.component_max_area_ratio:
                        continue
                if h_c > 0:
                    aspect = float(w_c) / float(h_c)
                    if aspect < self.config.component_aspect_min:
                        continue
                    if aspect > self.config.component_aspect_max:
                        continue
                    size_ratio = float(h_c) / label_h
                    if size_ratio < self.config.icon_size_min_ratio:
                        continue
                    if size_ratio > self.config.icon_size_max_ratio:
                        continue
                comp_cy = y0s + y + (h_c / 2.0)
                if abs(comp_cy - label_cy) > label_h * 1.4:
                    continue
                x1c = x + w_c
                y1c = y + h_c
                if x <= ux1 + merge_pad and x1c >= ux0 - merge_pad and y <= uy1 + merge_pad and y1c >= uy0 - merge_pad:
                    nx0 = min(ux0, x)
                    ny0 = min(uy0, y)
                    nx1 = max(ux1, x1c)
                    ny1 = max(uy1, y1c)
                    if (nx0, ny0, nx1, ny1) != (ux0, uy0, ux1, uy1):
                        ux0, uy0, ux1, uy1 = nx0, ny0, nx1, ny1
                        changed = True

        pad_px = max(2, int(self.config.padding_px * 0.8))
        x0i = max(0, x0s + ux0 - pad_px)
        y0i = max(0, y0s + uy0 - pad_px)
        x1i = min(w, x0s + ux1 + pad_px)
        y1i = min(h, y0s + uy1 + pad_px)

        if x1i <= x0i or y1i <= y0i:
            return None, None, 0.0

        pad_touch = max(2, int(label_h * self.config.icon_edge_pad_ratio))
        x0i, y0i, x1i, y1i = self._expand_bbox_if_touching(image_rgb, (x0i, y0i, x1i, y1i), pad_touch)
        crop = image_rgb[y0i:y1i, x0i:x1i]
        crop, inner = self._tight_crop_icon(crop)
        if crop is None or crop.size == 0:
            return None, None, 0.0
        if inner is not None:
            x0i = x0i + inner[0]
            y0i = y0i + inner[1]
            x1i = x0i + (inner[2] - inner[0])
            y1i = y0i + (inner[3] - inner[1])
        score = self._score_crop(crop)
        return crop, (float(x0i), float(y0i), float(x1i), float(y1i)), score

    def _find_icon_in_search_window(
        self, image_rgb: np.ndarray, label_box: Box, side: str
    ) -> Tuple[np.ndarray | None, Box | None, float]:
        if cv2 is None:
            return None, None, 0.0

        h, w, _ = image_rgb.shape
        x0, y0, x1, y1 = label_box
        label_h = max(1.0, y1 - y0)
        search_w = label_h * self.config.icon_search_scale_w
        search_h = label_h * self.config.icon_search_scale_h
        pad = self.config.padding_px

        yc = (y0 + y1) / 2.0
        y0s = yc - search_h / 2.0
        y1s = yc + search_h / 2.0

        if side == "left":
            x1s = x0 - pad
            x0s = x1s - search_w
        else:
            x0s = x1 + pad
            x1s = x0s + search_w

        x0s = int(max(0, x0s))
        y0s = int(max(0, y0s))
        x1s = int(min(w, x1s))
        y1s = int(min(h, y1s))

        if x1s <= x0s or y1s <= y0s:
            return None, None, 0.0

        search = image_rgb[y0s:y1s, x0s:x1s]
        gray = self._to_gray(search)

        # Otsu threshold (invert to make dark icon -> white)
        try:
            _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        except Exception:
            return None, None, 0.0

        kernel = np.ones((3, 3), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel, iterations=2)
        bw = cv2.dilate(bw, kernel, iterations=1)

        num_labels, _, stats, centroids = cv2.connectedComponentsWithStats(bw, connectivity=8)
        if num_labels <= 1:
            return None, None, 0.0

        search_area = float((y1s - y0s) * (x1s - x0s))
        best = None
        best_score = -1.0
        center_x = (x1s - x0s) / 2.0
        center_y = (y1s - y0s) / 2.0

        for i in range(1, num_labels):
            x, y, w_c, h_c, area = stats[i]
            if h_c == 0 or w_c == 0:
                continue
            area_ratio = float(area) / search_area
            if area_ratio < self.config.component_min_area_ratio:
                continue
            if area_ratio > self.config.component_max_area_ratio:
                continue
            aspect = float(w_c) / float(h_c)
            if aspect < self.config.component_aspect_min or aspect > self.config.component_aspect_max:
                continue

            cx, cy = centroids[i]
            dist = ((cx - center_x) ** 2 + (cy - center_y) ** 2) ** 0.5
            score = (area_ratio * 1000.0) - (dist * 0.5)
            if score > best_score:
                best_score = score
                best = (x, y, w_c, h_c)

        if best is None:
            return None, None, 0.0

        bx, by, bw_c, bh_c = best
        # Merge nearby components to avoid half-cropped icons
        merge_pad = max(2, int(min(search_w, search_h) * 0.06))
        ux0, uy0 = bx, by
        ux1, uy1 = bx + bw_c, by + bh_c
        for i in range(1, num_labels):
            x, y, w_c, h_c, area = stats[i]
            if h_c == 0 or w_c == 0:
                continue
            x1 = x + w_c
            y1 = y + h_c
            if x <= ux1 + merge_pad and x1 >= ux0 - merge_pad and y <= uy1 + merge_pad and y1 >= uy0 - merge_pad:
                ux0 = min(ux0, x)
                uy0 = min(uy0, y)
                ux1 = max(ux1, x1)
                uy1 = max(uy1, y1)

        pad_px = max(2, int(self.config.padding_px * 0.8))
        x0i = max(0, x0s + ux0 - pad_px)
        y0i = max(0, y0s + uy0 - pad_px)
        x1i = min(w, x0s + ux1 + pad_px)
        y1i = min(h, y0s + uy1 + pad_px)

        if x1i <= x0i or y1i <= y0i:
            return None, None, 0.0

        pad_touch = max(2, int(label_h * self.config.icon_edge_pad_ratio))
        x0i, y0i, x1i, y1i = self._expand_bbox_if_touching(image_rgb, (x0i, y0i, x1i, y1i), pad_touch)
        crop = image_rgb[y0i:y1i, x0i:x1i]
        crop, inner = self._tight_crop_icon(crop)
        if crop is None or crop.size == 0:
            return None, None, 0.0
        if inner is not None:
            x0i = x0i + inner[0]
            y0i = y0i + inner[1]
            x1i = x0i + (inner[2] - inner[0])
            y1i = y0i + (inner[3] - inner[1])
        score = self._score_crop(crop)
        return crop, (float(x0i), float(y0i), float(x1i), float(y1i)), score

    def _crop_icon(
        self,
        image_rgb: np.ndarray,
        label_box: Box,
        side: str,
        scale_mult: float = 1.0,
        pad_mult: float = 1.0,
    ) -> Tuple[np.ndarray | None, Box | None]:
        h, w, _ = image_rgb.shape
        x0, y0, x1, y1 = label_box
        label_h = max(1.0, y1 - y0)
        icon_size = label_h * self.config.icon_scale * scale_mult
        pad = self.config.padding_px * pad_mult

        yc = (y0 + y1) / 2.0
        y0i = yc - icon_size / 2.0
        y1i = yc + icon_size / 2.0

        if side == "left":
            x1i = x0 - pad
            x0i = x1i - icon_size
        else:
            x0i = x1 + pad
            x1i = x0i + icon_size

        x0i = int(max(0, x0i))
        y0i = int(max(0, y0i))
        x1i = int(min(w, x1i))
        y1i = int(min(h, y1i))

        if x1i <= x0i or y1i <= y0i:
            return None, None

        pad_touch = max(2, int(label_h * self.config.icon_edge_pad_ratio))
        x0i, y0i, x1i, y1i = self._expand_bbox_if_touching(image_rgb, (x0i, y0i, x1i, y1i), pad_touch)
        crop = image_rgb[y0i:y1i, x0i:x1i]
        crop, inner = self._tight_crop_icon(crop)
        if crop is None or crop.size == 0:
            return None, None
        if inner is not None:
            x0i = x0i + inner[0]
            y0i = y0i + inner[1]
            x1i = x0i + (inner[2] - inner[0])
            y1i = y0i + (inner[3] - inner[1])
        return crop, (float(x0i), float(y0i), float(x1i), float(y1i))

    def _tight_crop_icon(self, crop: np.ndarray | None) -> Tuple[np.ndarray | None, Tuple[int, int, int, int] | None]:
        if crop is None or crop.size == 0:
            return None, None
        h, w = crop.shape[:2]
        gray = self._to_gray(crop)
        ys, xs = np.where(gray < 245)
        if ys.size == 0 or xs.size == 0:
            return crop, (0, 0, w, h)
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        pad = int(max(h, w) * self.config.tight_crop_pad_ratio)
        x0 = max(0, x0 - pad)
        y0 = max(0, y0 - pad)
        x1 = min(w - 1, x1 + pad)
        y1 = min(h - 1, y1 + pad)
        return crop[y0 : y1 + 1, x0 : x1 + 1], (x0, y0, x1 + 1, y1 + 1)

    def _expand_bbox_if_touching(
        self, image_rgb: np.ndarray, bbox: Tuple[float, float, float, float], pad: int
    ) -> Tuple[int, int, int, int]:
        x0, y0, x1, y1 = bbox
        h_img, w_img = image_rgb.shape[:2]
        x0 = int(max(0, x0))
        y0 = int(max(0, y0))
        x1 = int(min(w_img, x1))
        y1 = int(min(h_img, y1))
        if x1 <= x0 or y1 <= y0:
            return x0, y0, x1, y1
        crop = image_rgb[y0:y1, x0:x1]
        if crop.size == 0:
            return x0, y0, x1, y1
        gray = self._to_gray(crop)
        ys, xs = np.where(gray < 245)
        if ys.size == 0 or xs.size == 0:
            return x0, y0, x1, y1
        left = xs.min() == 0
        right = xs.max() == (gray.shape[1] - 1)
        top = ys.min() == 0
        bottom = ys.max() == (gray.shape[0] - 1)
        if left:
            x0 = max(0, x0 - pad)
        if right:
            x1 = min(w_img, x1 + pad)
        if top:
            y0 = max(0, y0 - pad)
        if bottom:
            y1 = min(h_img, y1 + pad)
        return x0, y0, x1, y1

    def _ink_touches_edges(self, crop: np.ndarray) -> bool:
        if crop.size == 0:
            return False
        gray = self._to_gray(crop)
        ys, xs = np.where(gray < 245)
        if ys.size == 0 or xs.size == 0:
            return False
        h, w = gray.shape
        left = xs.min() == 0
        right = xs.max() == (w - 1)
        top = ys.min() == 0
        bottom = ys.max() == (h - 1)
        # treat any hard edge contact as a sign of clipping
        return left or right or top or bottom

    def _recrop_with_pad_until_clear(
        self, image_rgb: np.ndarray, bbox: Box, pad: int
    ) -> Tuple[np.ndarray, Box]:
        h_img, w_img = image_rgb.shape[:2]
        x0, y0, x1, y1 = bbox
        pad = max(2, pad)
        crop = None
        out_box = bbox
        for _ in range(2):
            x0i = max(0, int(x0 - pad))
            y0i = max(0, int(y0 - pad))
            x1i = min(w_img, int(x1 + pad))
            y1i = min(h_img, int(y1 + pad))
            crop = image_rgb[y0i:y1i, x0i:x1i]
            out_box = (float(x0i), float(y0i), float(x1i), float(y1i))
            if crop.size == 0 or not self._ink_touches_edges(crop):
                break
            pad = int(pad * 1.7) + 1
        return crop, out_box

    def _pad_crop_white(self, crop: np.ndarray, pad: int) -> np.ndarray:
        if crop.size == 0 or pad <= 0:
            return crop
        if cv2 is None:
            h, w = crop.shape[:2]
            out = np.full((h + pad * 2, w + pad * 2, 3), 255, dtype=crop.dtype)
            out[pad : pad + h, pad : pad + w] = crop
            return out
        return cv2.copyMakeBorder(
            crop, pad, pad, pad, pad, borderType=cv2.BORDER_CONSTANT, value=(255, 255, 255)
        )

    def _find_footer_icon_components(
        self, image_rgb: np.ndarray, footer_top_img: int, median_label_h: float
    ) -> List[Box]:
        if cv2 is None:
            return []
        h, w, _ = image_rgb.shape
        footer_top_img = int(max(0, min(h, footer_top_img)))
        footer = image_rgb[footer_top_img:, :]
        if footer.size == 0:
            return []
        gray = self._to_gray(footer)
        try:
            _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        except Exception:
            return []
        kernel = np.ones((3, 3), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel, iterations=2)
        bw = cv2.dilate(bw, kernel, iterations=1)

        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        if num_labels <= 1:
            return []

        min_area = max(20.0, (median_label_h * median_label_h) * 0.08)
        min_h = median_label_h * 0.4
        max_h = median_label_h * 2.5

        components: List[Box] = []
        for i in range(1, num_labels):
            x, y, w_c, h_c, area = stats[i]
            if area < min_area:
                continue
            if h_c < min_h or h_c > max_h:
                continue
            if h_c > 0:
                aspect = float(w_c) / float(h_c)
                if aspect < self.config.component_aspect_min:
                    continue
                if aspect > self.config.component_aspect_max:
                    continue
            x0 = float(x)
            y0 = float(y + footer_top_img)
            x1 = float(x + w_c)
            y1 = float(y + footer_top_img + h_c)
            # guard against footer artifacts at the very bottom edge
            if y1 >= h:
                y1 = float(h)
            components.append((x0, y0, x1, y1))
        return components

    def _pick_component_for_label(
        self,
        label_bbox: Box,
        components: List[Box],
        preferred_side: str | None,
        used_components: set[int],
        median_label_h: float,
    ) -> int | None:
        lx0, ly0, lx1, ly1 = label_bbox
        lcy = (ly0 + ly1) / 2.0
        best_idx = None
        best_score = None

        for i, box in enumerate(components):
            if i in used_components:
                continue
            x0, y0, x1, y1 = box
            ccy = (y0 + y1) / 2.0
            if abs(ccy - lcy) > (median_label_h * 1.6):
                continue

            if preferred_side == "left":
                if x1 > lx0:
                    continue
                dx = lx0 - x1
            elif preferred_side == "right":
                if x0 < lx1:
                    continue
                dx = x0 - lx1
            else:
                if x1 <= lx0:
                    dx = lx0 - x1
                elif x0 >= lx1:
                    dx = x0 - lx1
                else:
                    dx = 0.0

            if dx > (median_label_h * 8.0):
                continue

            score = dx + abs(ccy - lcy) * 0.2
            if best_score is None or score < best_score:
                best_score = score
                best_idx = i

        return best_idx

    def _crop_from_component(
        self, image_rgb: np.ndarray, bbox: Box, median_label_h: float
    ) -> Tuple[np.ndarray | None, Box | None]:
        h, w, _ = image_rgb.shape
        x0, y0, x1, y1 = bbox
        pad = max(2, int(self.config.padding_px * 0.8), int(median_label_h * 0.08))
        x0i = max(0, int(x0 - pad))
        y0i = max(0, int(y0 - pad))
        x1i = min(w, int(x1 + pad))
        y1i = min(h, int(y1 + pad))
        if x1i <= x0i or y1i <= y0i:
            return None, None
        x0i, y0i, x1i, y1i = self._expand_bbox_if_touching(
            image_rgb, (x0i, y0i, x1i, y1i), max(2, int(median_label_h * self.config.icon_edge_pad_ratio))
        )
        crop = image_rgb[y0i:y1i, x0i:x1i]
        crop, inner = self._tight_crop_icon(crop)
        if crop is None or crop.size == 0:
            return None, None
        if inner is not None:
            x0i = x0i + inner[0]
            y0i = y0i + inner[1]
            x1i = x0i + (inner[2] - inner[0])
            y1i = y0i + (inner[3] - inner[1])
        return crop, (float(x0i), float(y0i), float(x1i), float(y1i))

    def _score_crop(self, crop: np.ndarray | None) -> float:
        if crop is None or crop.size == 0:
            return 0.0
        gray = self._to_gray(crop)
        nonwhite_ratio = float(np.mean(gray < 245))
        if cv2 is not None:
            edges = cv2.Canny(gray, 50, 150)
            edge_ratio = float(np.mean(edges > 0))
            return nonwhite_ratio + (edge_ratio * 1.5)
        return nonwhite_ratio

    def _crop_valid(self, crop: np.ndarray | None) -> bool:
        if crop is None or crop.size == 0:
            return False
        gray = self._to_gray(crop)
        nonwhite_ratio = float(np.mean(gray < 245))
        if nonwhite_ratio < self.config.min_ink_ratio:
            return False
        if nonwhite_ratio > self.config.max_ink_ratio:
            return False
        if cv2 is not None:
            _, thresh = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY_INV)
            num_labels, _ = cv2.connectedComponents(thresh)
            # Too many components likely means text or noise
            if num_labels > 50:
                return False
        return True

    def _to_gray(self, crop: np.ndarray) -> np.ndarray:
        if cv2 is not None:
            return cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        # fallback
        return np.mean(crop, axis=2).astype(np.uint8)

    def _save_image(self, image_rgb: np.ndarray, path: Path) -> None:
        from PIL import Image, ImageFilter

        img = Image.fromarray(image_rgb)
        scale = float(self.config.icon_output_scale or 1.0)
        if scale > 1.0:
            w, h = img.size
            img = img.resize((int(w * scale), int(h * scale)), resample=Image.LANCZOS)
        if self.config.icon_output_sharpen:
            img = img.filter(ImageFilter.UnsharpMask(radius=1.6, percent=160, threshold=3))
        img.save(path)
