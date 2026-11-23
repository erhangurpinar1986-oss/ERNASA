"""ERNASA ATS isolated, authenticated, transient PDF OCR service.

No candidate identifiers, database connection, persistence, third-party OCR
provider or background processing. Only an authorized backend may request OCR.
"""
import hmac
import os
import subprocess
from io import BytesIO

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
        raise HTTPException(422, "Invalid PDF")
    try:
        pdf = pymupdf.open(stream=bytes(raw), filetype="pdf")
        if pdf.is_encrypted or not 1 <= pdf.page_count <= 60:
            raise HTTPException(422, "Unsupported PDF")
        texts = []
        with pdf:
            pages = min(pdf.page_count, MAX_PAGES)
            for index in range(pages):
                page = pdf[index]
                if page.rect.width <= 0 or page.rect.height <= 0 or page.rect.width * page.rect.height > 4000000:
                    raise HTTPException(422, "Invalid page dimensions")
                # Modest scale prevents runaway raster allocations on small instances.
                scale = min(1.45, (MAX_IMAGE_PIXELS / (page.rect.width * page.rect.height)) ** .5)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
                if pix.width * pix.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(422, "Image too large")
                png = pix.tobytes("png")
                result = subprocess.run(
                    ["tesseract", "stdin", "stdout", "-l", "tur+eng", "--psm", "3"],
                    input=png, capture_output=True, timeout=23,
                    env={"PATH": "/usr/bin:/bin", "OMP_THREAD_LIMIT": "1", "HOME": "/tmp"},
                    check=False,
                )
                if result.returncode:
                    raise HTTPException(422, "OCR unable to read page")
                texts.append(result.stdout.decode("utf-8", errors="replace").strip())
        text = "\n\n".join(t for t in texts if t)[:MAX_TEXT_CHARS]
        if len(text.strip()) < 35:
            raise HTTPException(422, "No readable text found")
        # OCR can be inaccurate: never trigger recruiting decisions from this response.
        return {"text": text, "pages_read": pages, "review_required": True}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "OCR timed out") from None
    except (ValueError, RuntimeError, pymupdf.FileDataError):
        raise HTTPException(422, "Unable to open PDF") from None
