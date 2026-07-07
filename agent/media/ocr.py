import asyncio
import re
import cv2
import numpy as np
import easyocr
from loguru import logger

# Reader is initialized once and reused — loading models on first call takes ~10s
_reader = None


def _get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        logger.info("Loading EasyOCR model (first time may take ~10s)...")
        _reader = easyocr.Reader(['en'], gpu=False)
        logger.info("EasyOCR model loaded")
    return _reader


def _clahe_preprocess(file_path: str) -> np.ndarray:
    """
    Apply CLAHE (Contrast Limited Adaptive Histogram Equalization) to improve
    text contrast in UPI screenshots before OCR.
    Works on the L channel in LAB space so colour is preserved.
    """
    img = cv2.imread(file_path)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    lab = cv2.merge((l, a, b))
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


async def extract_text_from_image(file_path: str) -> str:
    """Run EasyOCR in a thread pool to avoid blocking the event loop."""
    logger.info(f"Running OCR on: {file_path}")
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _run_ocr, file_path)
    logger.info(f"OCR complete — {len(text)} characters extracted")
    return text


def _run_ocr(file_path: str) -> str:
    reader = _get_reader()
    img = _clahe_preprocess(file_path)
    results = reader.readtext(img, detail=0)
    text = "\n".join(results).strip()
    return _fix_ocr(text)


def _fix_ocr(text: str) -> str:
    # EasyOCR commonly misreads the ₹ symbol as the digit "2".
    # A line that is purely "2" followed by digits is almost certainly
    # a currency amount — replace the leading "2" with "₹".
    fixed = []
    for line in text.split("\n"):
        stripped = line.strip()
        if re.match(r'^2[\d,]+(?:\.\d+)?$', stripped):
            line = line.replace(stripped, "₹" + stripped[1:], 1)
        fixed.append(line)
    return "\n".join(fixed)
