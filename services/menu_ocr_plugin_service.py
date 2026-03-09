"""
Menu OCR Plugin Service

Integration wrapper for the standalone OCR package under:
    ocrbadfinalfr/ocrbad

This service intentionally does not modify OCR code. It shells into the OCR
runtime and returns one-line fact suggestions suitable for Agent Builder KB.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class MenuOCRPluginService:
    """Safe wrapper around the standalone menu OCR pipeline."""

    SUPPORTED_EXTS: set[str] = {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
        ".csv",
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        ".ppt",
        ".pptx",
        ".odp",
        ".xls",
        ".xlsx",
        ".ods",
    }

    def __init__(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = repo_root
        self.plugin_root = repo_root / "ocrbadfinalfr" / "ocrbad"
        self.pipeline_file = self.plugin_root / "unified_menu_pipeline.py"
        log_root_env = str(os.getenv("MENU_OCR_LOG_ROOT", "")).strip()
        output_root_env = str(os.getenv("MENU_OCR_OUTPUT_ROOT", "")).strip()
        self.log_root = (
            Path(log_root_env).expanduser()
            if log_root_env
            else (Path("/tmp/menu_ocr_logs") if os.name != "nt" else (repo_root / "logs" / "menu_ocr"))
        )
        self.output_root = (
            Path(output_root_env).expanduser()
            if output_root_env
            else (Path("/tmp/menu_ocr_output") if os.name != "nt" else (self.plugin_root / "output"))
        )
        self.log_root = self._ensure_writable_dir(self.log_root, "menu_ocr_logs")
        self.output_root = self._ensure_writable_dir(self.output_root, "menu_ocr_output")

    @staticmethod
    def _ensure_writable_dir(path: Path, fallback_name: str) -> Path:
        candidate = Path(path).expanduser()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            fallback = Path(tempfile.gettempdir()) / fallback_name
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _tail_text(value: str | None, limit: int = 10000) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[-limit:]

    def _write_log(self, run_id: str, payload: dict[str, Any]) -> Path:
        self.log_root.mkdir(parents=True, exist_ok=True)
        log_path = self.log_root / f"{run_id}.json"
        log_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return log_path

    def list_recent_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        bounded = max(1, min(int(limit), 200))
        files = sorted(self.log_root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:bounded]
        for fp in files:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append(
                {
                    "run_id": data.get("run_id"),
                    "status": data.get("status"),
                    "started_at": data.get("started_at"),
                    "finished_at": data.get("finished_at"),
                    "duration_sec": data.get("duration_sec"),
                    "file_name": data.get("input", {}).get("name"),
                    "error_message": data.get("error", {}).get("message"),
                    "log_file": str(fp),
                }
            )
        return rows

    def get_log(self, run_id: str) -> dict[str, Any] | None:
        key = re.sub(r"[^a-zA-Z0-9_-]+", "", str(run_id or "")).strip()
        if not key:
            return None
        path = self.log_root / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _collect_partial_output_snapshot(self, file_path: Path, limit: int = 4) -> list[dict[str, Any]]:
        if not self.output_root.exists():
            return []
        stem_key = re.sub(r"[^a-z0-9]+", "_", file_path.stem.lower()).strip("_")
        if not stem_key:
            return []
        candidates = sorted(
            self.output_root.glob(f"{stem_key}_*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )[: max(1, min(int(limit), 10))]
        rows: list[dict[str, Any]] = []
        for out_dir in candidates:
            if not out_dir.exists() or not out_dir.is_dir():
                continue
            files = sorted([child.name for child in out_dir.iterdir() if child.is_file()])[:30]
            rows.append(
                {
                    "output_dir": str(out_dir),
                    "last_write_time_epoch": out_dir.stat().st_mtime,
                    "file_count": len([1 for child in out_dir.iterdir() if child.is_file()]),
                    "has_menu_formatted": (out_dir / "menu_formatted.json").exists(),
                    "has_openai_status": (out_dir / "openai_status.json").exists(),
                    "has_openai_usage": (out_dir / "openai_usage.json").exists(),
                    "files": files,
                }
            )
        return rows

    @staticmethod
    def _is_valid_item_name(text: str) -> bool:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return False
        if len(value) < 3:
            return False
        lowered = value.lower()
        blocked_substrings = (
            "in room dining",
            "served in",
            "kcal",
            " am",
            " pm",
            "to ",
            "allergens",
            "legend",
        )
        if any(token in lowered for token in blocked_substrings):
            return False
        if re.fullmatch(r"[\d\s.,/-]+", value):
            return False
        if re.fullmatch(r"[a-z]{1,4}", lowered):
            return False
        return True

    @staticmethod
    def _extract_price_token(text: str) -> str | None:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return None
        match = re.fullmatch(r"(?:rs\.?\s*)?(?:₹\s*)?(\d{2,5}(?:\.\d{1,2})?)", value, flags=re.IGNORECASE)
        if not match:
            return None
        return match.group(1)

    def _build_partial_result_from_raw_pages(
        self,
        *,
        file_path: Path,
        menu_name_hint: str,
        max_facts: int,
        started_epoch: float,
        run_id: str,
        run_log: dict[str, Any],
        plugin_status: dict[str, Any],
        use_openai_primary: bool,
        used_openai: bool,
        fallback_used: bool,
    ) -> dict[str, Any] | None:
        # Explicitly disabled: caller requires full pipeline output only.
        return None

    @staticmethod
    def _is_usable_python(python_exec: Path) -> bool:
        """Check if candidate interpreter can start and load OCR deps."""
        if not python_exec.exists():
            return False
        try:
            probe = subprocess.run(
                [
                    str(python_exec),
                    "-c",
                    (
                        "import sys; "
                        "import fitz, numpy, PIL, reportlab, cffi, openai; "
                        "print(sys.version_info[:2])"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=18,
            )
            return probe.returncode == 0
        except Exception:
            return False

    def _resolve_python_executable(self) -> Path:
        """Prefer OCR plugin venv python, fallback to working local interpreters."""
        env_python = os.getenv("MENU_OCR_PYTHON", "").strip()
        candidates = [
            Path(env_python) if env_python else None,
            self.repo_root / ".menu_ocr_runtime" / "Scripts" / "python.exe",
            self.repo_root / ".menu_ocr_runtime" / "bin" / "python",
            self.repo_root / ".menu_ocr_venv" / "Scripts" / "python.exe",
            self.repo_root / ".menu_ocr_venv" / "bin" / "python",
            self.plugin_root / ".venv" / "Scripts" / "python.exe",
            self.plugin_root / ".venv" / "bin" / "python",
            self.repo_root / "venv" / "Scripts" / "python.exe",
            self.repo_root / ".venv" / "Scripts" / "python.exe",
            Path(sys.executable),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            if self._is_usable_python(candidate):
                return candidate
        return Path(sys.executable)

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        raw = str(os.getenv(name, str(default))).strip()
        try:
            return int(raw)
        except Exception:
            return int(default)

    def _run_pipeline_subprocess(
        self,
        *,
        input_file: Path,
        result_file: Path,
        menu_name_hint: str,
        use_openai: bool,
        timeout_sec: int,
        openai_request_timeout_sec: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        script = (
            "import json\n"
            "from pathlib import Path\n"
            "from unified_menu_pipeline import run_unified_menu_pipeline\n"
            f"result = run_unified_menu_pipeline(Path(r'''{str(input_file)}'''), Path(r'''{str(self.output_root)}'''), dpi=350, use_openai={str(bool(use_openai))}, cleanup=True, menu_name_hint=r'''{menu_name_hint}''')\n"
            f"Path(r'''{str(result_file)}''').write_text(json.dumps(result), encoding='utf-8')\n"
        )
        cmd = [str(self._resolve_python_executable()), "-c", script]
        child_env = os.environ.copy()
        if openai_request_timeout_sec is not None:
            child_env["OPENAI_REQUEST_TIMEOUT_SEC"] = str(max(20, int(openai_request_timeout_sec)))
        return subprocess.run(
            cmd,
            cwd=str(self.plugin_root),
            capture_output=True,
            text=True,
            timeout=max(60, int(timeout_sec)),
            env=child_env,
        )

    def get_status(self) -> dict[str, Any]:
        python_exec = self._resolve_python_executable()
        return {
            "available": bool(self.plugin_root.exists() and self.pipeline_file.exists()),
            "plugin_root": str(self.plugin_root),
            "pipeline_file": str(self.pipeline_file),
            "python_executable": str(python_exec),
            "python_exists": python_exec.exists(),
            "output_root": str(self.output_root),
        }

    @staticmethod
    def _to_one_line(value: Any, max_len: int = 220) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return ""
        if len(text) > max_len:
            text = text[: max_len - 1].rstrip()
        if not text.endswith("."):
            text = f"{text}."
        return text

    @classmethod
    def _build_fact_lines(cls, menu_formatted: dict[str, Any], max_facts: int = 100) -> list[str]:
        """Convert OCR formatted menu output into strict one-line fact sentences."""
        max_facts = max(1, min(int(max_facts), 300))
        lines: list[str] = []
        seen: set[str] = set()

        def add(line: str) -> None:
            normalized = cls._to_one_line(line)
            if not normalized:
                return
            key = normalized.lower()
            if key in seen:
                return
            seen.add(key)
            lines.append(normalized)

        menu_name = str(menu_formatted.get("menu_name") or "").strip()
        if menu_name:
            add(f"Menu name is {menu_name}")

        notes = menu_formatted.get("notes", [])
        if isinstance(notes, list):
            for note in notes[:5]:
                note_text = cls._to_one_line(note, max_len=180)
                if note_text:
                    add(note_text)

        items = menu_formatted.get("items", [])
        if isinstance(items, list):
            for item in items:
                if len(lines) >= max_facts:
                    break
                if not isinstance(item, dict):
                    continue

                name = cls._to_one_line(item.get("name"), max_len=120).rstrip(".")
                if not name:
                    continue

                price = str(item.get("price") or "").strip()
                dish_type = str(item.get("dish_type") or "").strip()
                description = cls._to_one_line(item.get("description"), max_len=150).rstrip(".")

                if price and dish_type:
                    add(f"{name} ({dish_type}) is priced at {price}")
                elif price:
                    add(f"{name} is priced at {price}")
                elif description:
                    add(f"{name}: {description}")
                else:
                    add(name)

        return lines[:max_facts]

    def scan_menu(
        self,
        upload_path: Path,
        menu_name_hint: str = "",
        max_facts: int = 100,
        timeout_sec: int = 7200,
    ) -> dict[str, Any]:
        """
        Run OCR pipeline for one menu file and return builder-friendly fact lines.
        """
        run_id = f"menu_ocr_{uuid4().hex}"
        started_epoch = time.time()
        run_log: dict[str, Any] = {
            "run_id": run_id,
            "started_at": self._now_iso(),
            "status": "running",
            "input": {},
            "runtime": {},
            "subprocess_runs": [],
        }

        file_path = Path(upload_path).resolve()
        if not file_path.exists() or not file_path.is_file():
            err = ValueError(f"Menu file not found: {file_path}")
            run_log["status"] = "error"
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise ValueError(f"{err} [trace_id={run_id}] [log={log_path}]")

        ext = file_path.suffix.lower()
        if ext not in self.SUPPORTED_EXTS:
            err = ValueError(f"Unsupported menu file type: {ext or '(no extension)'}")
            run_log["status"] = "error"
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["input"] = {
                "path": str(file_path),
                "name": file_path.name,
                "extension": ext,
                "size_bytes": file_path.stat().st_size if file_path.exists() else None,
            }
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise ValueError(f"{err} [trace_id={run_id}] [log={log_path}]")

        status = self.get_status()
        run_log["input"] = {
            "path": str(file_path),
            "name": file_path.name,
            "extension": ext,
            "size_bytes": file_path.stat().st_size if file_path.exists() else None,
            "menu_name_hint": str(menu_name_hint or ""),
            "max_facts": int(max_facts),
            "timeout_sec": int(timeout_sec),
        }
        run_log["runtime"]["plugin_status"] = status
        if not status.get("available"):
            err = ValueError("Menu OCR plugin folder/pipeline not found")
            run_log["status"] = "error"
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise ValueError(f"{err} [trace_id={run_id}] [log={log_path}]")
        if not status.get("python_exists"):
            err = ValueError("Menu OCR plugin python runtime not found")
            run_log["status"] = "error"
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise ValueError(f"{err} [trace_id={run_id}] [log={log_path}]")

        self.output_root.mkdir(parents=True, exist_ok=True)
        result_file = self.output_root / f"builder_result_{run_id}.json"
        use_openai_primary = self._env_flag("MENU_OCR_USE_OPENAI", default=True)
        used_openai = use_openai_primary
        fallback_used = False
        total_timeout_sec = max(
            240,
            min(int(timeout_sec), self._env_int("MENU_OCR_TOTAL_MAX_SEC", 21600)),
        )
        openai_max_sec = max(
            60,
            min(total_timeout_sec, self._env_int("MENU_OCR_OPENAI_MAX_SEC", total_timeout_sec)),
        )
        fallback_max_sec = max(
            120,
            min(total_timeout_sec, self._env_int("MENU_OCR_FALLBACK_MAX_SEC", total_timeout_sec)),
        )
        openai_request_timeout_sec = max(
            60,
            self._env_int("MENU_OCR_OPENAI_REQUEST_TIMEOUT_SEC", 300),
        )
        run_log["runtime"].update(
            {
                "use_openai_primary": use_openai_primary,
                "total_timeout_sec": total_timeout_sec,
                "openai_max_sec": openai_max_sec,
                "fallback_max_sec": fallback_max_sec,
                "openai_request_timeout_sec": openai_request_timeout_sec,
            }
        )
        started_at = time.perf_counter()
        proc: subprocess.CompletedProcess[str] | None = None
        openai_timed_out = False
        openai_failed = False

        if use_openai_primary:
            try:
                call_started = time.perf_counter()
                proc = self._run_pipeline_subprocess(
                    input_file=file_path,
                    result_file=result_file,
                    menu_name_hint=menu_name_hint,
                    use_openai=True,
                    timeout_sec=openai_max_sec,
                    openai_request_timeout_sec=openai_request_timeout_sec,
                )
                run_log["subprocess_runs"].append(
                    {
                        "mode": "openai",
                        "returncode": proc.returncode,
                        "duration_sec": round(time.perf_counter() - call_started, 2),
                        "stdout_tail": self._tail_text(proc.stdout),
                        "stderr_tail": self._tail_text(proc.stderr),
                    }
                )
                openai_failed = proc.returncode != 0
            except subprocess.TimeoutExpired:
                openai_timed_out = True
                openai_failed = True
                proc = None
                run_log["subprocess_runs"].append(
                    {
                        "mode": "openai",
                        "timed_out": True,
                        "timeout_sec": openai_max_sec,
                    }
                )
            except Exception as exc:
                err = RuntimeError(f"Failed to run Menu OCR plugin: {exc}")
                partial = self._build_partial_result_from_raw_pages(
                    file_path=file_path,
                    menu_name_hint=menu_name_hint,
                    max_facts=max_facts,
                    started_epoch=started_epoch,
                    run_id=run_id,
                    run_log=run_log,
                    plugin_status=status,
                    use_openai_primary=use_openai_primary,
                    used_openai=used_openai,
                    fallback_used=fallback_used,
                )
                if partial is not None:
                    return partial
                run_log["status"] = "error"
                run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
                run_log["error"] = {
                    "type": type(err).__name__,
                    "message": str(err),
                    "traceback": traceback.format_exc(),
                }
                run_log["finished_at"] = self._now_iso()
                run_log["duration_sec"] = round(time.time() - started_epoch, 2)
                log_path = self._write_log(run_id, run_log)
                raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]") from exc
        else:
            openai_failed = True

        if openai_failed:
            fallback_used = True
            used_openai = False
            elapsed = int(time.perf_counter() - started_at)
            remaining_budget = max(120, total_timeout_sec - elapsed)
            remaining_timeout = max(120, min(fallback_max_sec, remaining_budget))
            try:
                call_started = time.perf_counter()
                proc = self._run_pipeline_subprocess(
                    input_file=file_path,
                    result_file=result_file,
                    menu_name_hint=menu_name_hint,
                    use_openai=False,
                    timeout_sec=remaining_timeout,
                    openai_request_timeout_sec=None,
                )
                run_log["subprocess_runs"].append(
                    {
                        "mode": "deterministic_fallback",
                        "returncode": proc.returncode,
                        "duration_sec": round(time.perf_counter() - call_started, 2),
                        "stdout_tail": self._tail_text(proc.stdout),
                        "stderr_tail": self._tail_text(proc.stderr),
                    }
                )
            except subprocess.TimeoutExpired as exc:
                msg = (
                    "Menu OCR scan timed out during OpenAI pass and fallback pass"
                    if openai_timed_out
                    else "Menu OCR scan timed out"
                )
                run_log["subprocess_runs"].append(
                    {
                        "mode": "deterministic_fallback",
                        "timed_out": True,
                        "timeout_sec": remaining_timeout,
                    }
                )
                partial = self._build_partial_result_from_raw_pages(
                    file_path=file_path,
                    menu_name_hint=menu_name_hint,
                    max_facts=max_facts,
                    started_epoch=started_epoch,
                    run_id=run_id,
                    run_log=run_log,
                    plugin_status=status,
                    use_openai_primary=use_openai_primary,
                    used_openai=used_openai,
                    fallback_used=fallback_used,
                )
                if partial is not None:
                    return partial
                run_log["status"] = "error"
                run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
                run_log["error"] = {"type": "TimeoutExpired", "message": msg}
                run_log["finished_at"] = self._now_iso()
                run_log["duration_sec"] = round(time.time() - started_epoch, 2)
                log_path = self._write_log(run_id, run_log)
                if openai_timed_out:
                    raise RuntimeError(
                        "Menu OCR scan timed out during OpenAI pass and fallback pass"
                        f" [trace_id={run_id}] [log={log_path}]"
                    ) from exc
                raise RuntimeError(f"Menu OCR scan timed out [trace_id={run_id}] [log={log_path}]") from exc
            except Exception as exc:
                err = RuntimeError(f"Failed to run Menu OCR plugin fallback: {exc}")
                partial = self._build_partial_result_from_raw_pages(
                    file_path=file_path,
                    menu_name_hint=menu_name_hint,
                    max_facts=max_facts,
                    started_epoch=started_epoch,
                    run_id=run_id,
                    run_log=run_log,
                    plugin_status=status,
                    use_openai_primary=use_openai_primary,
                    used_openai=used_openai,
                    fallback_used=fallback_used,
                )
                if partial is not None:
                    return partial
                run_log["status"] = "error"
                run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
                run_log["error"] = {
                    "type": type(err).__name__,
                    "message": str(err),
                    "traceback": traceback.format_exc(),
                }
                run_log["finished_at"] = self._now_iso()
                run_log["duration_sec"] = round(time.time() - started_epoch, 2)
                log_path = self._write_log(run_id, run_log)
                raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]") from exc

        assert proc is not None
        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "").strip()[-1800:]
            stdout_tail = (proc.stdout or "").strip()[-1800:]
            detail = stderr_tail or stdout_tail or f"exit code {proc.returncode}"
            err = RuntimeError(f"Menu OCR plugin failed: {detail}")
            partial = self._build_partial_result_from_raw_pages(
                file_path=file_path,
                menu_name_hint=menu_name_hint,
                max_facts=max_facts,
                started_epoch=started_epoch,
                run_id=run_id,
                run_log=run_log,
                plugin_status=status,
                use_openai_primary=use_openai_primary,
                used_openai=used_openai,
                fallback_used=fallback_used,
            )
            if partial is not None:
                return partial
            run_log["status"] = "error"
            run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]")

        if not result_file.exists():
            err = RuntimeError("Menu OCR plugin did not return result metadata")
            partial = self._build_partial_result_from_raw_pages(
                file_path=file_path,
                menu_name_hint=menu_name_hint,
                max_facts=max_facts,
                started_epoch=started_epoch,
                run_id=run_id,
                run_log=run_log,
                plugin_status=status,
                use_openai_primary=use_openai_primary,
                used_openai=used_openai,
                fallback_used=fallback_used,
            )
            if partial is not None:
                return partial
            run_log["status"] = "error"
            run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]")

        try:
            result_payload = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception as exc:
            err = RuntimeError("Menu OCR plugin returned invalid result metadata")
            partial = self._build_partial_result_from_raw_pages(
                file_path=file_path,
                menu_name_hint=menu_name_hint,
                max_facts=max_facts,
                started_epoch=started_epoch,
                run_id=run_id,
                run_log=run_log,
                plugin_status=status,
                use_openai_primary=use_openai_primary,
                used_openai=used_openai,
                fallback_used=fallback_used,
            )
            if partial is not None:
                return partial
            run_log["status"] = "error"
            run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
            run_log["error"] = {
                "type": type(err).__name__,
                "message": str(err),
                "traceback": traceback.format_exc(),
            }
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]") from exc
        finally:
            try:
                result_file.unlink(missing_ok=True)
            except Exception:
                pass

        output_dir_raw = str(result_payload.get("output_dir") or "").strip()
        output_dir = Path(output_dir_raw) if output_dir_raw else None
        if not output_dir or not output_dir.exists():
            err = RuntimeError("Menu OCR output folder not found after scan")
            partial = self._build_partial_result_from_raw_pages(
                file_path=file_path,
                menu_name_hint=menu_name_hint,
                max_facts=max_facts,
                started_epoch=started_epoch,
                run_id=run_id,
                run_log=run_log,
                plugin_status=status,
                use_openai_primary=use_openai_primary,
                used_openai=used_openai,
                fallback_used=fallback_used,
            )
            if partial is not None:
                return partial
            run_log["status"] = "error"
            run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]")

        menu_formatted_path_raw = str(result_payload.get("menu_formatted") or "").strip()
        menu_formatted_path = Path(menu_formatted_path_raw) if menu_formatted_path_raw else (output_dir / "menu_formatted.json")
        if not menu_formatted_path.exists():
            err = RuntimeError("menu_formatted.json was not generated by OCR plugin")
            partial = self._build_partial_result_from_raw_pages(
                file_path=file_path,
                menu_name_hint=menu_name_hint,
                max_facts=max_facts,
                started_epoch=started_epoch,
                run_id=run_id,
                run_log=run_log,
                plugin_status=status,
                use_openai_primary=use_openai_primary,
                used_openai=used_openai,
                fallback_used=fallback_used,
            )
            if partial is not None:
                return partial
            run_log["status"] = "error"
            run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]")

        try:
            menu_formatted = json.loads(menu_formatted_path.read_text(encoding="utf-8"))
        except Exception as exc:
            err = RuntimeError("Failed to parse menu_formatted.json")
            partial = self._build_partial_result_from_raw_pages(
                file_path=file_path,
                menu_name_hint=menu_name_hint,
                max_facts=max_facts,
                started_epoch=started_epoch,
                run_id=run_id,
                run_log=run_log,
                plugin_status=status,
                use_openai_primary=use_openai_primary,
                used_openai=used_openai,
                fallback_used=fallback_used,
            )
            if partial is not None:
                return partial
            run_log["status"] = "error"
            run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
            run_log["error"] = {
                "type": type(err).__name__,
                "message": str(err),
                "traceback": traceback.format_exc(),
            }
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]") from exc
        if not isinstance(menu_formatted, dict):
            err = RuntimeError("menu_formatted.json has invalid structure")
            partial = self._build_partial_result_from_raw_pages(
                file_path=file_path,
                menu_name_hint=menu_name_hint,
                max_facts=max_facts,
                started_epoch=started_epoch,
                run_id=run_id,
                run_log=run_log,
                plugin_status=status,
                use_openai_primary=use_openai_primary,
                used_openai=used_openai,
                fallback_used=fallback_used,
            )
            if partial is not None:
                return partial
            run_log["status"] = "error"
            run_log["partial_output_snapshot"] = self._collect_partial_output_snapshot(file_path)
            run_log["error"] = {"type": type(err).__name__, "message": str(err)}
            run_log["finished_at"] = self._now_iso()
            run_log["duration_sec"] = round(time.time() - started_epoch, 2)
            log_path = self._write_log(run_id, run_log)
            raise RuntimeError(f"{err} [trace_id={run_id}] [log={log_path}]")

        items = menu_formatted.get("items", [])
        items_count = len(items) if isinstance(items, list) else 0
        priced_count = 0
        if isinstance(items, list):
            priced_count = len([item for item in items if isinstance(item, dict) and str(item.get("price") or "").strip()])

        fact_lines = self._build_fact_lines(menu_formatted, max_facts=max_facts)
        duration_sec = round(time.perf_counter() - started_at, 2)
        run_log["status"] = "ok"
        run_log["finished_at"] = self._now_iso()
        run_log["duration_sec"] = round(time.time() - started_epoch, 2)
        run_log["result_summary"] = {
            "menu_name": str(menu_formatted.get("menu_name") or "").strip() or None,
            "items_total": items_count,
            "items_with_price": priced_count,
            "fact_count": len(fact_lines),
            "output_dir": str(output_dir.resolve()),
            "menu_formatted_path": str(menu_formatted_path.resolve()),
            "duration_sec": duration_sec,
            "openai_requested": use_openai_primary,
            "openai_used": used_openai,
            "openai_fallback_used": fallback_used,
        }
        log_path = self._write_log(run_id, run_log)

        return {
            "status": "ok",
            "fact_lines": fact_lines,
            "ocr_raw_output": menu_formatted,
            "ocr_raw_output_text": json.dumps(menu_formatted, indent=2, ensure_ascii=False),
            "summary": {
                "menu_name": str(menu_formatted.get("menu_name") or "").strip() or None,
                "items_total": items_count,
                "items_with_price": priced_count,
                "fact_count": len(fact_lines),
                "output_dir": str(output_dir.resolve()),
                "menu_formatted_path": str(menu_formatted_path.resolve()),
                "duration_sec": duration_sec,
                "openai_requested": use_openai_primary,
                "openai_used": used_openai,
                "openai_fallback_used": fallback_used,
            },
            "plugin_status": status,
            "trace": {
                "run_id": run_id,
                "log_file": str(log_path),
            },
        }


menu_ocr_plugin_service = MenuOCRPluginService()
