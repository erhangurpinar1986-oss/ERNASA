"""ERNASA ATS isolated, authenticated, transient PDF OCR service.

No candidate identifiers, database connection, persistence, third-party OCR
provider or background processing. Only an authorized backend may request OCR.
"""
import hmac
import os
import subprocess

import pymupdf
from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="ERNASA OCR Internal", docs_url=None, redoc_url=None, openapi_url=None)
MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 3
MAX_IMAGE_PIXELS = 5_000_000
MAX_TEXT_CHARS = 16000


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract(request: Request):
    expected = os.getenv("ERNASA_OCR_SHARED_KEY", "")
    received = request.headers.get("x-ernasa-ocr-key", "")
    if len(expected) < 32 or not hmac.compare_digest(received, expected):
        raise HTTPException(403, "Forbidden")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/pdf":
        raise HTTPException(415, "PDF required")
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "File too large")
    if not bytes(raw).startswith(b"%PDF-"):
        raise HTTPException(422, "invalid_pdf")
    try:
        pdf = pymupdf.open(stream=bytes(raw), filetype="pdf")
        if pdf.is_encrypted:
            raise HTTPException(422, "encrypted_pdf")
        if not 1 <= pdf.page_count <= 60:
            raise HTTPException(422, "unsupported_pdf")
        texts = []
        with pdf:
            pages = min(pdf.page_count, MAX_PAGES)
            for index in range(pages):
                page = pdf[index]
                if page.rect.width <= 0 or page.rect.height <= 0 or page.rect.width * page.rect.height > 4000000:
                    raise HTTPException(422, "invalid_page_dimensions")
                # Modest scale prevents runaway raster allocations on small instances.
                scale = min(2.0, (MAX_IMAGE_PIXELS / (page.rect.width * page.rect.height)) ** .5)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
                if pix.width * pix.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(422, "image_too_large")
                png = pix.tobytes("png")
                # Sparse text mode handles CV layouts with columns and text boxes.
                # A secondary layout mode is tried only when the first is empty.
                extracted = ""
                for mode in ("11", "6"):
                    result = subprocess.run(
                        ["tesseract", "stdin", "stdout", "-l", "tur+eng", "--psm", mode],
                        input=png, capture_output=True, timeout=18,
                        env={"PATH": "/usr/bin:/bin", "OMP_THREAD_LIMIT": "1", "HOME": "/tmp"},
                        check=False,
                    )
                    if result.returncode:
                        raise HTTPException(422, "ocr_engine_failed")
                    candidate = result.stdout.decode("utf-8", errors="replace").strip()
                    if len(candidate) > len(extracted):
                        extracted = candidate
                    if len(extracted) >= 35:
                        break
                if extracted:
                    texts.append(extracted)
        text = "\n\n".join(t for t in texts if t)[:MAX_TEXT_CHARS]
        if len(text.strip()) < 35:
            raise HTTPException(422, "no_text_detected")
        # OCR can be inaccurate: never trigger recruiting decisions from this response.
        return {"text": text, "pages_read": pages, "review_required": True}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "ocr_timeout") from None
    except (ValueError, RuntimeError, pymupdf.FileDataError):
        raise HTTPException(422, "invalid_pdf") from None
