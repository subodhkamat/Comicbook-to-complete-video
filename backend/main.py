
from pathlib import Path
from uuid import uuid4
import io
from pydantic import BaseModel, HttpUrl
from comic_reader_service import (import_comic_reader, import_downloadable_comic)
import pymupdf
from PIL import Image, UnidentifiedImageError
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from ocr_service import extract_comic_text
from narration_service import generate_narration
import shutil
class DownloadableComicRequest(BaseModel):
    url: HttpUrl

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"

JOBS_DIR.mkdir(parents=True, exist_ok=True)
import asyncio
import sys

print("Python:", sys.version)
print("Loop policy:", type(asyncio.get_event_loop_policy()).__name__)

try:
    loop = asyncio.get_running_loop()
    print("Running loop:", type(loop).__name__)
except RuntimeError as exc:
    print("No running loop:", exc)


app = FastAPI(title="Comic AI Video Creator", version="0.1.0")

app.mount(
    "/files",
    StaticFiles(directory=str(JOBS_DIR)),
    name="files",
)

MAX_UPLOAD_SIZE = 50 * 1024 * 1024
ALLOWED_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}


@app.get("/")
def home():
    return {
        "app": "Comic AI Video Creator",
        "status": "running",
        "version": "0.1.0",
    }


@app.post("/upload-comic")
async def upload_comic(file: UploadFile = File(...)):
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()

    if extension != ".pdf" and extension not in ALLOWED_IMAGES:
        raise HTTPException(
            status_code=400,
            detail="Upload a PDF, PNG, JPG, JPEG, or WEBP file.",
        )

    content = await file.read(MAX_UPLOAD_SIZE + 1)

    if not content:
        raise HTTPException(status_code=400, detail="File is empty.")

    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Maximum upload size is 50 MB.",
        )

    job_id = uuid4().hex
    pages_dir = JOBS_DIR / job_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    try:
        if extension == ".pdf":
            pdf = pymupdf.open(stream=content, filetype="pdf")

            if pdf.page_count == 0:
                raise HTTPException(
                    status_code=400,
                    detail="The PDF contains no pages.",
                )

            if pdf.page_count > 200:
                raise HTTPException(
                    status_code=400,
                    detail="Maximum supported length is 200 pages.",
                )

            for index, page in enumerate(pdf):
                pix = page.get_pixmap(
                    matrix=pymupdf.Matrix(1.5, 1.5),
                    alpha=False,
                )
                pix.save(str(pages_dir / f"page-{index + 1:03}.png"))

            page_count = pdf.page_count
            pdf.close()

        else:
            image = Image.open(io.BytesIO(content))
            image.load()

            if image.width * image.height > 40_000_000:
                raise HTTPException(
                    status_code=400,
                    detail="Image resolution is too large.",
                )

            image.convert("RGB").save(
                pages_dir / "page-001.png",
                format="PNG",
            )
            page_count = 1

    except HTTPException:
        raise
    except (pymupdf.FileDataError, UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="The file is invalid or cannot be decoded.",
        )

    return {
        "job_id": job_id,
        "page_count": page_count,
        "pages": [
            f"/files/{job_id}/pages/page-{i:03}.png"
            for i in range(1, page_count + 1)
        ],
        "next_step": "comic_text_extraction",
    }


class ComicReaderURLRequest(BaseModel):
    url: HttpUrl



import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)

@app.post("/import-reader-url")
async def import_reader_comic(request: ComicReaderURLRequest):
    try:
        return await import_comic_reader(str(request.url))

    except ValueError as exc:
        logger.exception("Comic import validation failed")
        raise HTTPException(
            status_code=400,
            detail=str(exc) or repr(exc),
        ) from exc

    except Exception as exc:
        logger.exception("Comic import failed")
        raise HTTPException(
            status_code=500,
            detail=f"{type(exc).__name__}: {str(exc) or repr(exc)}",
        ) from exc


@app.post("/extract-text/{job_id}")
async def extract_text_endpoint(job_id: str):
    import asyncio

    try:
        return await asyncio.to_thread(
            extract_comic_text,
            job_id,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        import logging
        logging.exception("Comic OCR failed")

        raise HTTPException(
            status_code=500,
            detail="Text extraction failed. Check backend logs.",
        ) from exc


@app.post("/generate-narration/{job_id}")
async def generate_narration_endpoint(job_id: str):
    import asyncio
    import logging

    try:
        return await asyncio.to_thread(
            generate_narration,
            job_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logging.exception("Narration generation failed")
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc

@app.post("/import-downloadable-comic")
async def import_downloadable_comic_endpoint(
    request: DownloadableComicRequest,
):
    try:
        return await import_downloadable_comic(str(request.url))

    except ValueError as exc:
        logger.exception("Downloadable comic import validation failed")
        raise HTTPException(
            status_code=400,
            detail=str(exc) or repr(exc),
        ) from exc

    except Exception as exc:
        logger.exception("Downloadable comic import failed")
        raise HTTPException(
            status_code=500,
            detail=f"{type(exc).__name__}: {str(exc) or repr(exc)}",
        ) from exc


JOBS_DIR = BASE_DIR / "data" / "jobs"


@app.delete("/delete-job/{job_id}")
async def delete_job_files(job_id: str):
    """Delete all files belonging to a specific job."""

    # Validate the job ID to prevent path traversal.
    if not job_id or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for char in job_id
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid job ID",
        )

    job_dir = (JOBS_DIR / job_id).resolve()
    jobs_root = JOBS_DIR.resolve()

    if job_dir.parent != jobs_root:
        raise HTTPException(
            status_code=400,
            detail="Invalid job directory",
        )

    if not job_dir.exists() or not job_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail="Job files not found",
        )

    try:
        shutil.rmtree(job_dir)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not delete job files: {exc}",
        ) from exc

    return {
        "success": True,
        "job_id": job_id,
        "message": "All files for this job have been deleted.",
    }
