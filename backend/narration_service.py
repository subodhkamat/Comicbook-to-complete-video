
import json
import os
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from groq import Groq

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "data" / "jobs"

load_dotenv(BASE_DIR / ".env")

_client = None


def get_client():
    global _client

    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is missing from backend/.env")

        _client = Groq(api_key=api_key)

    return _client


def generate_narration(job_id: str) -> dict:
    try:
        UUID(job_id)
    except ValueError as exc:
        raise ValueError("Invalid job ID.") from exc

    job_dir = (JOBS_DIR / job_id).resolve()

    if not job_dir.is_relative_to(JOBS_DIR.resolve()):
        raise ValueError("Invalid job directory.")

    ocr_path = job_dir / "ocr_results.json"

    if not ocr_path.is_file():
        raise ValueError(
            "OCR results not found. Run extract-text first."
        )

    with ocr_path.open("r", encoding="utf-8") as file:
        ocr_data = json.load(file)

    pages = ocr_data.get("pages", [])

    if not pages:
        raise ValueError("OCR results contain no pages.")

    client = get_client()
    narration_pages = []

    for page in pages:
        page_number = page.get("page_number")
        comic_text = (page.get("full_text") or "").strip()

        if not comic_text:
            narration = (
                "The story continues on this page, "
                "but no readable dialogue was detected."
            )
        else:
            response = client.chat.completions.create(
                model=os.getenv(
                    "GROQ_MODEL",
                    "llama-3.3-70b-versatile",
                ),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You write engaging narration for comic "
                            "videos. Rewrite the supplied OCR text "
                            "as natural spoken narration. Preserve "
                            "the original meaning, character names, "
                            "events, and dialogue. Do not invent "
                            "plot details. Keep it concise and easy "
                            "to speak aloud. Return only the narration."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Comic page: {page_number}\n"
                            f"Detected text:\n{comic_text}"
                        ),
                    },
                ],
                temperature=0.4,
                max_tokens=350,
            )

            narration = (
                response.choices[0].message.content or ""
            ).strip()

            if not narration:
                raise RuntimeError(
                    f"No narration returned for page {page_number}."
                )

        narration_pages.append({
            "page_number": page_number,
            "source_text": comic_text,
            "narration": narration,
        })

    output = {
        "job_id": job_id,
        "page_count": len(narration_pages),
        "pages": narration_pages,
    }

    output_path = job_dir / "narration.json"
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        **output,
        "results_file": f"/files/{job_id}/narration.json",
    }
