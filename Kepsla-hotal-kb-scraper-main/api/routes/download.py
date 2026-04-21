"""API endpoints for downloading generated KB files."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from config.settings import settings
from models.job import ScrapeJob, JobStatus, async_session
from services.job_state import reconcile_completed_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _select_job_zip_path(job: ScrapeJob, job_output_dir: Path) -> Path | None:
    """Pick the ZIP for the current bundle, otherwise fall back to the newest archive."""
    output_dir = (job.output_dir or "").strip()
    if output_dir:
        bundle_dir = Path(output_dir)
        expected_zip = job_output_dir / f"{bundle_dir.name}.zip"
        if expected_zip.exists():
            return expected_zip

    zip_files = sorted(
        job_output_dir.glob("*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not zip_files:
        return None

    return zip_files[0]


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/download/{job_id}")
async def download_job_zip(job_id: str):
    """Download a ZIP archive containing all KB files for a completed job.

    Returns a ``404`` if the job does not exist or has not completed yet,
    and a ``400`` if the job is still in progress.
    """
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job:
            await reconcile_completed_job(session, job)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check job status
    if job.status == JobStatus.FAILED.value:
        raise HTTPException(
            status_code=400,
            detail=f"Job failed: {job.error_message or 'Unknown error'}",
        )

    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(
            status_code=400,
            detail=f"Job is still in progress (status: {job.status}, {job.progress_pct}%)",
        )

    # Locate the ZIP file in output/{job_id}/
    job_output_dir = Path(settings.output_dir) / job_id

    if not job_output_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="Output directory not found for this job",
        )

    zip_path = _select_job_zip_path(job, job_output_dir)
    if zip_path is None:
        raise HTTPException(
            status_code=404,
            detail="ZIP file not found in job output directory",
        )

    logger.info("Serving ZIP download for job %s: %s", job_id, zip_path.name)

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=zip_path.name,
        headers={"Content-Disposition": f'attachment; filename="{zip_path.name}"'},
    )


@router.get("/download/{job_id}/{filename}")
async def download_specific_file(job_id: str, filename: str):
    """Download a specific KB file from a job's output directory.

    The file is searched recursively within ``output/{job_id}/``.

    Security: filenames containing ``..`` are rejected to prevent path
    traversal attacks.
    """
    # Security: prevent path traversal
    if ".." in filename:
        raise HTTPException(
            status_code=400,
            detail="Invalid filename: path traversal not allowed",
        )

    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Locate the job output directory
    job_output_dir = Path(settings.output_dir) / job_id

    if not job_output_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="Output directory not found for this job",
        )

    # Search recursively for the requested filename
    matching_files = list(job_output_dir.rglob(filename))

    if not matching_files:
        raise HTTPException(
            status_code=404,
            detail=f"File '{filename}' not found in job output",
        )

    file_path = matching_files[0]

    # Double-check the resolved path is still inside the output directory
    # (defence in depth against symlink attacks)
    try:
        file_path.resolve().relative_to(job_output_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid file path",
        )

    # Determine media type from file extension
    media_type_map = {
        ".txt": "text/plain",
        ".json": "application/json",
        ".md": "text/markdown",
        ".zip": "application/zip",
    }
    suffix = file_path.suffix.lower()
    media_type = media_type_map.get(suffix, "application/octet-stream")

    logger.info(
        "Serving file download for job %s: %s (%s)",
        job_id, file_path.name, media_type,
    )

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
    )
