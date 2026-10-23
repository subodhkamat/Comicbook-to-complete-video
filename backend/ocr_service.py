
import json
from pathlib import Path
from uuid import UUID

import easyocr

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "data" / "jobs"

# Load the model once instead of once per request.
_reader = None


def get_reader():
    global _reader

    if _reader is None:
        _reader = easyocr.Reader(
            ["en"],
            gpu=False,
        )

    return _reader


def find_page_images(job_id: str) -> list[Path]:
    # Validate the job identifier before constructing paths.
    try:
        UUID(job_id)
    except ValueError as exc:
        raise ValueError("Invalid job ID.") from exc

    job_dir = (JOBS_DIR / job_id).resolve()

    if not job_dir.is_relative_to(JOBS_DIR.resolve()):
        raise ValueError("Invalid job directory.")

    if not job_dir.is_dir():
        raise ValueError("Job not found.")

    # Support both layouts used by Phase 1.
    candidates = [
        *job_dir.glob("page_*.png"),
        *job_dir.glob("page_*.jpg"),
        *job_dir.glob("page_*.jpeg"),
        *job_dir.glob("pages/page-*.png"),
        *job_dir.glob("pages/page-*.jpg"),
        *job_dir.glob("pages/page-*.jpeg"),
    ]

    # Keep a stable, natural page order.
    def page_number(path: Path) -> int:
        digits = "".join(
            character
            for character in path.stem.split("-")[-1].split("_")[-1]
            if character.isdigit()
        )
        return int(digits) if digits else 0

    pages = sorted(
        set(candidates),
        key=page_number,
    )

    if not pages:
        raise ValueError("No supported page images found for this job.")

    return pages


def extract_comic_text(job_id: str) -> dict:
    pages = find_page_images(job_id)
    reader = get_reader()

    results = []

    for index, image_path in enumerate(pages, start=1):
        detections = reader.readtext(
            str(image_path),
            detail=1,
            paragraph=False,
        )

        text_regions = []

        for bbox, text, confidence in detections:
            text = text.strip()

            if not text:
                continue

            text_regions.append({
                "text": text,
                "confidence": round(float(confidence), 4),
                "bbox": [
                    [round(float(x), 2), round(float(y), 2)]
                    for x, y in bbox
                ],
            })

        results.append({
            "page_number": index,
            "image": image_path.name,
            "text_regions": text_regions,
            "full_text": "\n".join(
                region["text"] for region in text_regions
            ),
        })

    output_path = JOBS_DIR / job_id / "ocr_results.json"

    output_path.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "page_count": len(results),
                "pages": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "job_id": job_id,
        "page_count": len(results),
        "total_text_regions": sum(
            len(page["text_regions"]) for page in results
        ),
        "results_file": f"/files/{job_id}/ocr_results.json",
        "pages": results,
    }
