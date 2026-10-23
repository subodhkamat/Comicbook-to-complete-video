
import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pymupdf
from PIL import Image, UnidentifiedImageError
from io import BytesIO


BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "data" / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024
MAX_PAGES = 100
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


def validate_public_url(url: str) -> None:
    """Reject unsafe URL schemes, hosts, and private IP destinations."""
    parsed = urlsplit(url)

    if parsed.scheme != "https":
        raise ValueError("Only HTTPS comic links are supported.")

    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid comic URL.")

    if parsed.port not in (None, 443):
        raise ValueError("Only the default HTTPS port is supported.")

    host = parsed.hostname

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            records = socket.getaddrinfo(
                host, 443, type=socket.SOCK_STREAM
            )
            addresses = list({
                ipaddress.ip_address(record[4][0])
                for record in records
            })
        except socket.gaierror as exc:
            raise ValueError("Could not resolve the comic URL.") from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("URL must resolve only to public IP addresses.")


async def download_comic(url: str) -> tuple[bytes, str]:
    validate_public_url(url)

    parsed = urlsplit(url)
    filename = Path(parsed.path).name
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(
            "Use a direct PDF or PNG, JPG, JPEG, or WebP image URL."
        )

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(20.0, connect=8.0),
        trust_env=False,
    ) as client:
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                raise ValueError(
                    "This URL redirects. Please provide the final "
                    "direct HTTPS file URL."
                )

            response.raise_for_status()

            content_type = response.headers.get(
                "content-type", ""
            ).split(";")[0].strip().lower()

            if content_type not in {
                "application/pdf",
                "image/png",
                "image/jpeg",
                "image/webp",
                "application/octet-stream",
            }:
                raise ValueError(
                    "The URL does not return a supported image or PDF."
                )

            length = response.headers.get("content-length")
            if length and int(length) > MAX_DOWNLOAD_SIZE:
                raise ValueError("Comic download exceeds 50 MB.")

            chunks = []
            total = 0

            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_SIZE:
                    raise ValueError("Comic download exceeds 50 MB.")
                chunks.append(chunk)

    return b"".join(chunks), extension


def save_comic_pages(
    content: bytes,
    extension: str,
) -> dict:
    job_id = str(uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    pages = []

    try:
        if extension == ".pdf":
            with pymupdf.open(stream=content, filetype="pdf") as pdf:
                if pdf.page_count < 1 or pdf.page_count > MAX_PAGES:
                    raise ValueError(
                        f"PDF must contain 1–{MAX_PAGES} pages."
                    )

                for index, page in enumerate(pdf):
                    pix = page.get_pixmap(
                        matrix=pymupdf.Matrix(1.5, 1.5),
                        alpha=False,
                    )
                    output = job_dir / f"page_{index + 1:03}.png"
                    pix.save(str(output))
                    pages.append(output.name)

        else:
            with Image.open(BytesIO(content)) as image:
                image.load()

                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Comic image dimensions are too large.")

                image.convert("RGB").save(
                    job_dir / "page_001.png",
                    format="PNG",
                )
                pages.append("page_001.png")

        return {
            "job_id": job_id,
            "page_count": len(pages),
            "pages": [
                f"/files/{job_id}/{name}" for name in pages
            ],
            "source_type": "pdf" if extension == ".pdf" else "image",
        }

    except Exception:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


async def import_comic_url(url: str) -> dict:
    content, extension = await download_comic(url)
    return await asyncio.to_thread(
        save_comic_pages, content, extension
    )
