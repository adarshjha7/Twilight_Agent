import asyncio
import pdfplumber
from loguru import logger


async def extract_text_from_pdf(file_path: str) -> str:
    """Extract text from all pages of a PDF, non-blocking."""
    logger.info(f"Parsing PDF: {file_path}")
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _read_pdf, file_path)
    logger.info(f"PDF parsed — {len(text)} characters extracted")
    return text


def _read_pdf(file_path: str) -> str:
    pages = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            pages.append(page_text)
    return "\n".join(pages).strip()
