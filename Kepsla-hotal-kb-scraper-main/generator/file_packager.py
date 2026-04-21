"""Package KB files into downloadable text bundles and save to the output directory.

Writes the plain-text KB artifact for each property, keeps validation in
memory for status reporting, and optionally bundles everything
into a ZIP archive for multi-property scrapes.

Output directory structure:
    {settings.output_dir}/{job_id}/{bundle_name}/{safe_property_name}/
        {property_name}_kb.txt
"""

import asyncio
import logging
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiofiles

from config.settings import settings
from generator.kb_formatter import format_txt
from generator.kb_validator import generate_validation_summary, validate_kb

logger = logging.getLogger(__name__)


def safe_filename(name: str) -> str:
    """Convert a property name into a filesystem-safe filename."""
    if not name or not name.strip():
        return "unknown_property"

    safe = name.strip().lower()
    safe = re.sub(r"[\s\-]+", "_", safe)
    safe = re.sub(r"[^a-z0-9_]", "", safe)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "unknown_property"


def build_job_bundle_name(
    job_label: str,
    extracted_at: datetime | None = None,
) -> str:
    """Build the user-facing bundle folder and ZIP name."""
    timestamp = (extracted_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).strftime("%Y%m%d_%H%M%S")
    safe_label = safe_filename(job_label or "hotel_kb")
    return f"{safe_label}_{timestamp}"


def _merge_existing_property_artifacts(
    legacy_property_dir: Path,
    target_property_dir: Path,
) -> None:
    """Merge pre-existing property files into the named bundle directory."""
    if not legacy_property_dir.exists():
        return

    try:
        if legacy_property_dir.resolve() == target_property_dir.resolve():
            return
    except OSError:
        return

    target_property_dir.mkdir(parents=True, exist_ok=True)

    for item in legacy_property_dir.iterdir():
        destination = target_property_dir / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)

    shutil.rmtree(legacy_property_dir, ignore_errors=True)


def _build_combined_kb_text(
    package_candidates: list[tuple[int, str, str]],
    packaged_properties_by_index: list[dict | None],
    source_url: str,
) -> str:
    """Build a single plain-text artifact containing all packaged KBs."""
    combined_sections: list[str] = []

    for index, property_name, kb_text in package_candidates:
        packaged = packaged_properties_by_index[index]
        if not packaged or packaged.get("error"):
            continue

        combined_sections.append(
            format_txt(kb_text, property_name, source_url).strip()
        )

    if not combined_sections:
        return ""

    separator = "\n\n" + ("=" * 80) + "\n\n"
    return separator.join(combined_sections) + "\n"


async def package_kb(
    property_name: str,
    source_url: str,
    kb_text: str,
    offer_kb_text: str,
    job_id: str,
    base_output_dir: Path | None = None,
) -> dict:
    """Package a single property's KB as a plain-text artifact."""
    safe_name = safe_filename(property_name)
    output_root = Path(base_output_dir) if base_output_dir else Path(settings.output_dir) / job_id
    output_dir = output_root / safe_name
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Packaging KB for '%s' (job=%s) into %s",
        property_name,
        job_id,
        output_dir,
    )

    txt_content = format_txt(kb_text, property_name, source_url)

    validation_report = validate_kb(kb_text, source_url)
    validation_summary = generate_validation_summary(validation_report)
    logger.info("Validation for '%s': %s", property_name, validation_summary.split("\n")[0])

    files = {
        "txt": output_dir / f"{safe_name}_kb.txt",
    }

    file_contents = {
        "txt": txt_content,
    }

    if offer_kb_text and offer_kb_text.strip():
        files["offers_txt"] = output_dir / f"{safe_name}_offers_kb.txt"
        file_contents["offers_txt"] = format_txt(
            offer_kb_text,
            f"{property_name} Offers",
            source_url,
        )

    for key, filepath in files.items():
        content = file_contents[key]
        try:
            async with aiofiles.open(filepath, mode="w", encoding="utf-8") as handle:
                await handle.write(content)
            logger.debug("Written %s (%d chars)", filepath.name, len(content))
        except OSError as exc:
            logger.error("Failed to write %s: %s", filepath, exc)
            raise

    result = {
        "property_name": property_name,
        "output_dir": str(output_dir.resolve()),
        "files": {key: str(path.resolve()) for key, path in files.items()},
        "validation": validation_report,
        "kb_preview": kb_text[:2000] if kb_text else "",
    }

    logger.info(
        "KB packaging complete for '%s': %d text file written to %s",
        property_name,
        len(files),
        output_dir,
    )

    return result


async def package_all_properties(
    properties_results: list[dict],
    job_id: str,
    source_url: str,
    job_label: str | None = None,
    extracted_at: datetime | None = None,
    progress_callback: Callable[[int, int, str, dict[str, Any]], Awaitable[None]] | None = None,
) -> dict:
    """Package KB files for all properties from a multi-property scrape."""
    logger.info(
        "Packaging %d properties for job '%s'",
        len(properties_results),
        job_id,
    )

    job_output_dir = Path(settings.output_dir) / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)
    bundle_name = build_job_bundle_name(job_label or "hotel_kb", extracted_at)
    bundle_output_dir = job_output_dir / bundle_name
    bundle_output_dir.mkdir(parents=True, exist_ok=True)

    packaged_properties_by_index: list[dict | None] = [None] * len(properties_results)
    package_candidates: list[tuple[int, str, str]] = []

    for index, prop in enumerate(properties_results):
        prop_name = prop.get("name", "Unknown Hotel")
        prop_kb_text = prop.get("kb_text", "")
        if not prop_kb_text or not prop_kb_text.strip():
            logger.warning("Skipping property '%s' - empty KB text", prop_name)
            continue
        package_candidates.append((index, prop_name, prop_kb_text))

    total_candidates = len(package_candidates)
    completed = 0
    semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_property_packaging))

    async def _package_candidate(index: int, prop_name: str, prop_kb_text: str) -> None:
        nonlocal completed

        async with semaphore:
            try:
                result = await package_kb(
                    property_name=prop_name,
                    source_url=source_url,
                    kb_text=prop_kb_text,
                    offer_kb_text=properties_results[index].get("offer_kb_text", ""),
                    job_id=job_id,
                    base_output_dir=bundle_output_dir,
                )
                _merge_existing_property_artifacts(
                    legacy_property_dir=job_output_dir / safe_filename(prop_name),
                    target_property_dir=Path(result["output_dir"]),
                )
            except Exception as exc:
                logger.error(
                    "Failed to package property '%s': %s",
                    prop_name,
                    exc,
                    exc_info=True,
                )
                result = {
                    "property_name": prop_name,
                    "error": str(exc),
                    "output_dir": None,
                    "files": {},
                    "validation": {"is_valid": False, "warnings": [str(exc)]},
                    "kb_preview": "",
                }

        packaged_properties_by_index[index] = result
        completed += 1

        if progress_callback:
            await progress_callback(completed, total_candidates, prop_name, result)

    await asyncio.gather(
        *[
            _package_candidate(index, prop_name, prop_kb_text)
            for index, prop_name, prop_kb_text in package_candidates
        ]
    )

    packaged_properties = [
        result for result in packaged_properties_by_index if result is not None
    ]
    total_files = sum(len(result.get("files", {})) for result in packaged_properties)

    combined_txt_path: Path | None = None
    combined_txt_content = _build_combined_kb_text(
        package_candidates=package_candidates,
        packaged_properties_by_index=packaged_properties_by_index,
        source_url=source_url,
    )

    if combined_txt_content:
        combined_txt_path = job_output_dir / f"{bundle_name}_all_kbs.txt"
        async with aiofiles.open(combined_txt_path, mode="w", encoding="utf-8") as handle:
            await handle.write(combined_txt_content)
        total_files += 1

    zip_path = job_output_dir / f"{bundle_name}.zip"

    try:
        _create_bundle_zip(zip_path, job_output_dir, bundle_output_dir, combined_txt_path)
        logger.info("ZIP archive created: %s", zip_path)
    except Exception as exc:
        logger.error("Failed to create ZIP archive: %s", exc, exc_info=True)
        zip_path = None

    result = {
        "job_id": job_id,
        "total_properties": len(packaged_properties),
        "properties": packaged_properties,
        "output_dir": str(bundle_output_dir.resolve()),
        "zip_file": str(zip_path.resolve()) if zip_path else None,
        "combined_txt_file": str(combined_txt_path.resolve()) if combined_txt_path else None,
        "total_files": total_files,
    }

    logger.info(
        "All properties packaged: %d properties, %d files, zip=%s",
        len(packaged_properties),
        total_files,
        "yes" if zip_path else "no",
    )

    return result


def _create_zip(
    zip_path: Path,
    job_output_dir: Path,
    packaged_properties: list[dict],
) -> None:
    """Create a ZIP archive containing all generated KB files."""
    del packaged_properties

    job_output_dir = job_output_dir.resolve()
    zip_path = zip_path.resolve()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(job_output_dir.rglob("*")):
            if not file_path.is_file():
                continue

            resolved = file_path.resolve()
            if resolved == zip_path:
                continue

            arcname = resolved.relative_to(job_output_dir)
            archive.write(resolved, arcname=str(arcname))

    logger.debug("ZIP archive written: %s", zip_path)


def _create_bundle_zip(
    zip_path: Path,
    job_output_dir: Path,
    bundle_output_dir: Path,
    combined_txt_path: Path | None,
) -> None:
    """Create a ZIP archive containing only the current bundle and its combined text file."""
    job_output_dir = job_output_dir.resolve()
    bundle_output_dir = bundle_output_dir.resolve()
    zip_path = zip_path.resolve()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(bundle_output_dir.rglob("*")):
            if not file_path.is_file():
                continue

            resolved = file_path.resolve()
            arcname = resolved.relative_to(job_output_dir)
            archive.write(resolved, arcname=str(arcname))

        if combined_txt_path and combined_txt_path.exists():
            resolved = combined_txt_path.resolve()
            arcname = resolved.relative_to(job_output_dir)
            archive.write(resolved, arcname=str(arcname))

    logger.debug("Bundle ZIP archive written: %s", zip_path)
