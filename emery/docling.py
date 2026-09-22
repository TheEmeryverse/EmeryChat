import asyncio
import base64
import os
import logging
import re
from urllib.parse import urlparse, unquote

import requests

import emery.globals as globals
from emery.config import ENABLE_DOCLING, DOCLING_URL, DOCLING_BEARER_TOKEN
from emery.logging_utils import safe_preview


SUPPORTED_DOCUMENT_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}

SUPPORTED_DOCUMENT_EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
}

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
VISUAL_FALLBACK_MAX_SECONDS = 90
VISUAL_FALLBACK_PAGE_TIMEOUT_SECONDS = 35
VISUAL_RENDER_SCALE = 2.0
VISUAL_RENDER_MAX_DIM = 2200
VISUAL_RENDER_JPEG_QUALITY = 88
_VISUAL_QUESTION_TERMS = {
    "color", "colour", "blue", "green", "red", "yellow", "route", "map",
    "chart", "graph", "diagram", "calendar", "schedule", "highlighted",
    "shaded", "table", "row", "column", "form", "checkbox", "icon",
    "image", "picture", "layout", "visual", "pictured", "shown",
}
_QUESTION_STOPWORDS = {
    "about", "after", "also", "are", "can", "does", "from", "have",
    "into", "just", "more", "need", "that", "the", "this", "what",
    "when", "where", "which", "with", "would", "you", "your",
}


def _normalize_content_type(content_type: str | None) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


def _extension_from_name(name: str | None) -> str:
    if not name:
        return ""
    _, ext = os.path.splitext(str(name).strip().lower())
    return ext


def detect_supported_document_type(
    filename: str | None = None,
    mime_type: str | None = None,
    content_type: str | None = None,
    url: str | None = None,
) -> str | None:
    for value in (mime_type, content_type):
        normalized = _normalize_content_type(value)
        if normalized in SUPPORTED_DOCUMENT_MIME_TYPES:
            return SUPPORTED_DOCUMENT_MIME_TYPES[normalized]

    for candidate in (filename, unquote(urlparse(url).path) if url else None):
        ext = _extension_from_name(candidate)
        if ext in SUPPORTED_DOCUMENT_EXTENSIONS:
            return SUPPORTED_DOCUMENT_EXTENSIONS[ext]

    return None


def _docling_headers() -> dict[str, str]:
    headers = {"accept": "application/json"}
    if DOCLING_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {DOCLING_BEARER_TOKEN}"
    return headers


def _build_docling_options(document_type: str) -> dict[str, object]:
    return {
        "from_formats": [document_type],
        # Markdown preserves headings, table boundaries, and page breaks better
        # than plain text. JSON gives us structured document metadata for quality
        # checks and future consumers without making the model read raw JSON.
        "to_formats": ["md", "json", "text"],
        "do_ocr": True,
        "force_ocr": False,
        "ocr_engine": "easyocr",
        "ocr_lang": ["en"],
        "pdf_backend": "dlparse_v2",
        "table_mode": "accurate",
        "table_cell_matching": True,
        "do_table_structure": True,
        "include_images": True,
        "images_scale": 1.5,
        "image_export_mode": "placeholder",
        "md_page_break_placeholder": "\n\n<!-- page-break -->\n\n",
        "abort_on_error": False,
    }


def _flatten_docling_options(document_type: str) -> list[tuple[str, str]]:
    data: list[tuple[str, str]] = []
    for key, value in _build_docling_options(document_type).items():
        if isinstance(value, list):
            data.extend((key, str(item).lower() if isinstance(item, bool) else str(item)) for item in value)
        else:
            data.append((key, str(value).lower() if isinstance(value, bool) else str(value)))
    return data


def _post_docling_file_sync(url: str, headers: dict[str, str], data: list[tuple[str, str]], files: dict):
    return requests.post(url, headers=headers, data=data, files=files, timeout=300, verify=False)


def _clean_extracted_content(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r'!\[[^\]]*\]\(data:image/[^)]*\)', '\n[Embedded image omitted]\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'!\[[^\]]*\]\([^)]*\)', '\n[Embedded image omitted]\n', cleaned)
    cleaned = re.sub(r'<img\b[^>]*>', '\n[Embedded image omitted]\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+', '\n[Embedded image omitted]\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    return cleaned.strip()


def _error_text(error) -> str:
    if isinstance(error, dict):
        return str(error.get("error_message") or error.get("message") or error)
    return str(error)


def _page_chunks(text: str) -> list[dict[str, object]]:
    clean_text = _clean_extracted_content(text)
    if not clean_text:
        return []
    pieces = re.split(r'\n\s*(?:<!--\s*page-break\s*-->|\f)\s*\n', clean_text, flags=re.IGNORECASE)
    chunks = []
    for index, piece in enumerate(pieces, start=1):
        piece = piece.strip()
        if piece:
            chunks.append({"page": index, "text": piece})
    return chunks or [{"page": 1, "text": clean_text}]


def _document_json_features(document_json) -> tuple[int | None, bool, bool]:
    """Return best-effort page/image/table metadata across Docling JSON versions."""
    if not isinstance(document_json, dict):
        return None, False, False
    pages = document_json.get("pages")
    page_count = len(pages) if isinstance(pages, (list, dict)) else None
    pictures = document_json.get("pictures") or document_json.get("images")
    tables = document_json.get("tables")
    return page_count, bool(pictures), bool(tables)


def _normalize_docling_result(
    payload: dict,
    source_name: str,
    source_type: str,
) -> dict:
    document = payload.get("document") or {}
    if not isinstance(document, dict):
        document = {}
    status = str(payload.get("status") or "failure").strip().lower()
    errors = payload.get("errors") or []
    markdown = _clean_extracted_content(document.get("md_content") or "")
    plain_text = _clean_extracted_content(document.get("text_content") or "")
    success = status in {"success", "partial_success"} and bool(markdown or plain_text)
    preferred_text = markdown or plain_text
    page_chunks = _page_chunks(preferred_text)
    json_content = document.get("json_content")
    json_page_count, has_images, has_tables = _document_json_features(json_content)
    page_count = json_page_count or len(page_chunks) or None
    text_chars = len(preferred_text)

    return {
        "success": success,
        "source_name": source_name,
        "source_type": source_type,
        "docling_status": status,
        "markdown": markdown,
        "plain_text": plain_text,
        "json_content": json_content,
        "page_chunks": page_chunks,
        "page_count": page_count,
        "text_chars": text_chars,
        "has_images": has_images,
        "has_tables": has_tables,
        "visual_fallback_recommended": source_type == "pdf" and (
            text_chars < 400 or has_images or bool(errors) or status != "success"
        ),
        "processing_time": payload.get("processing_time"),
        "errors": [_error_text(item) for item in errors if item],
    }


def _fallback_docling_result(source_name: str, source_type: str, error: str) -> dict:
    return {
        "success": False,
        "source_name": source_name,
        "source_type": source_type,
        "docling_status": "failure",
        "markdown": "",
        "plain_text": "",
        "json_content": None,
        "page_chunks": [],
        "page_count": None,
        "text_chars": 0,
        "has_images": False,
        "has_tables": False,
        "visual_fallback_recommended": source_type == "pdf",
        "processing_time": None,
        "errors": [error] if error else [],
    }


async def convert_document_bytes(
    file_bytes: bytes,
    filename: str,
    mime_type: str | None = None,
    source_url: str | None = None,
) -> dict:
    document_type = detect_supported_document_type(filename=filename, mime_type=mime_type)
    logging.info(
        "📄 DOCLING: upload detection filename=%s mime=%s -> type=%s enabled=%s url=%s",
        filename,
        mime_type,
        document_type,
        ENABLE_DOCLING,
        DOCLING_URL or "(unset)",
    )
    if not ENABLE_DOCLING or not DOCLING_URL:
        return _fallback_docling_result(filename or "document", document_type or "unknown", "Docling is not enabled.")
    if not document_type:
        return _fallback_docling_result(filename or "document", "unknown", "Unsupported document type.")
    if len(file_bytes or b"") > MAX_DOCUMENT_BYTES:
        return _fallback_docling_result(
            filename or "document",
            document_type,
            f"Document exceeds the {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB safety limit.",
        )
    if not file_bytes:
        return _fallback_docling_result(filename or "document", document_type, "Document is empty.")

    url = DOCLING_URL.rstrip("/") + "/v1/convert/file"
    data = _flatten_docling_options(document_type)
    files = {
        "files": (filename or f"upload.{document_type}", file_bytes, mime_type or "application/octet-stream")
    }

    try:
        logging.info(
            "📄 DOCLING: uploading filename=%s type=%s bytes=%s endpoint=%s",
            filename,
            document_type,
            len(file_bytes),
            url,
        )
        response = await asyncio.to_thread(
            _post_docling_file_sync,
            url,
            _docling_headers(),
            data,
            files,
        )
        if response.status_code != 200:
            logging.warning(
                "⚠️ DOCLING: upload failed filename=%s type=%s status=%s body=%s",
                filename,
                document_type,
                response.status_code,
                safe_preview(response.text, max_len=240),
            )
            return _fallback_docling_result(
                filename or "document",
                document_type,
                f"Docling returned HTTP {response.status_code}: {safe_preview(response.text, max_len=240)}",
            )
        normalized = _normalize_docling_result(response.json(), filename or "document", document_type)
        if source_url:
            normalized["source_url"] = source_url
        logging.info(
            "📄 DOCLING: upload success filename=%s status=%s markdown_chars=%s text_chars=%s errors=%s",
            filename,
            normalized.get("docling_status"),
            len(normalized.get("markdown") or ""),
            len(normalized.get("plain_text") or ""),
            len(normalized.get("errors") or []),
        )
        return normalized
    except Exception as exc:
        logging.warning("⚠️ DOCLING: file conversion failed for %s: %s", filename, exc)
        return _fallback_docling_result(filename or "document", document_type, f"Docling conversion failed: {exc}")


async def convert_document_url(url: str, filename: str | None = None, content_type: str | None = None) -> dict:
    document_type = detect_supported_document_type(filename=filename, content_type=content_type, url=url)
    source_name = filename or unquote(urlparse(url).path.rsplit("/", 1)[-1]) or url
    logging.info(
        "📄 DOCLING: source detection url=%s content_type=%s -> type=%s enabled=%s server=%s",
        safe_preview(url, max_len=180),
        content_type,
        document_type,
        ENABLE_DOCLING,
        DOCLING_URL or "(unset)",
    )
    if not ENABLE_DOCLING or not DOCLING_URL:
        return _fallback_docling_result(source_name, document_type or "unknown", "Docling is not enabled.")
    if not document_type:
        return _fallback_docling_result(source_name, "unknown", "Unsupported document type.")

    target_url = DOCLING_URL.rstrip("/") + "/v1/convert/source"
    payload = {
        "options": _build_docling_options(document_type),
        # Docling Serve's stable v1 API replaced the legacy http_sources
        # field with a discriminated sources list.
        "sources": [{"kind": "http", "url": url}],
    }

    try:
        logging.info(
            "📄 DOCLING: fetching remote document source_name=%s type=%s endpoint=%s",
            source_name,
            document_type,
            target_url,
        )
        response = await globals.http_client.post(
            target_url,
            headers={**_docling_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=300,
        )
        if response.status_code != 200:
            logging.warning(
                "⚠️ DOCLING: source conversion failed source=%s type=%s status=%s body=%s",
                source_name,
                document_type,
                response.status_code,
                safe_preview(response.text, max_len=240),
            )
            return _fallback_docling_result(
                source_name,
                document_type,
                f"Docling returned HTTP {response.status_code}: {safe_preview(response.text, max_len=240)}",
            )
        normalized = _normalize_docling_result(response.json(), source_name, document_type)
        normalized["source_url"] = url
        logging.info(
            "📄 DOCLING: source success source=%s status=%s markdown_chars=%s text_chars=%s errors=%s",
            source_name,
            normalized.get("docling_status"),
            len(normalized.get("markdown") or ""),
            len(normalized.get("plain_text") or ""),
            len(normalized.get("errors") or []),
        )
        return normalized
    except Exception as exc:
        logging.warning("⚠️ DOCLING: URL conversion failed for %s: %s", url, exc)
        return _fallback_docling_result(source_name, document_type, f"Docling conversion failed: {exc}")


def _question_terms(question: str | None) -> set[str]:
    return {
        term for term in re.findall(r"[a-z0-9]{3,}", str(question or "").lower())
        if term not in _QUESTION_STOPWORDS
    }


def _pdf_fill_color_name(fill) -> str | None:
    """Turn a PDF drawing fill into a stable, human-readable color label."""
    if not isinstance(fill, (tuple, list)) or len(fill) < 3:
        return None
    try:
        red, green, blue = (float(fill[0]), float(fill[1]), float(fill[2]))
    except (TypeError, ValueError):
        return None
    if max(red, green, blue) - min(red, green, blue) < 0.12:
        return None
    if blue >= 0.55 and red < 0.45 and green < 0.75:
        return "blue"
    if red >= 0.75 and green >= 0.65 and blue < 0.45:
        return "yellow"
    if red >= 0.65 and green < 0.45 and blue < 0.45:
        return "red"
    if green >= 0.5 and red < 0.55 and blue < 0.55:
        return "green"
    if blue >= 0.5 and red >= 0.45 and green < 0.55:
        return "purple"
    return "colored"


def _pdf_word_center(word: tuple) -> tuple[float, float]:
    return ((float(word[0]) + float(word[2])) / 2.0, (float(word[1]) + float(word[3])) / 2.0)


def _pdf_rect_contains(rect, point: tuple[float, float], padding: float = 0.0) -> bool:
    if not rect:
        return False
    x, y = point
    return (
        float(rect.x0) - padding <= x <= float(rect.x1) + padding
        and float(rect.y0) - padding <= y <= float(rect.y1) + padding
    )


def _pdf_native_visual_facts(page, question: str | None = None) -> str:
    """Extract PDF-native color/layout evidence before asking a vision model.

    Many schedules and maps store their colors as vector fills. Those fills are
    more reliable than asking a vision model to infer a color from a full-page
    raster, so preserve the color-to-text relationship as explicit evidence.
    This is intentionally document-generic: it describes colored text cells and
    nearby headings without assuming a calendar or any other document type.
    """
    try:
        words = [word for word in page.get_text("words") if len(word) >= 5 and str(word[4]).strip()]
        drawings = []
        for drawing in page.get_drawings():
            color_name = _pdf_fill_color_name(drawing.get("fill"))
            rect = drawing.get("rect")
            if color_name and rect and float(rect.width) > 1 and float(rect.height) > 1:
                drawings.append((rect, color_name))
        if not drawings:
            return "No PDF-native colored vector fills were detected."

        question_terms = _question_terms(question)
        heading_words = []
        for word in words:
            text = str(word[4]).strip()
            normalized = re.sub(r"[^a-z0-9]", "", text.lower())
            if len(normalized) >= 3 and (text.isupper() or normalized in question_terms):
                heading_words.append(word)

        def heading_for(word):
            cx, cy = _pdf_word_center(word)
            candidates = []
            for heading in heading_words:
                hx, hy = _pdf_word_center(heading)
                if hy > cy + 2 or abs(hx - cx) > 140:
                    continue
                distance = (cy - hy) + abs(cx - hx) * 0.35
                candidates.append((distance, str(heading[4]).strip()))
            return min(candidates)[1] if candidates else "page"

        # A colored drawing may span several adjacent cells. Assign each text
        # word to the smallest containing fill so every colored cell is retained.
        colored_words = []
        for word in words:
            center = _pdf_word_center(word)
            matches = [(rect.width * rect.height, rect, color_name) for rect, color_name in drawings if _pdf_rect_contains(rect, center, padding=0.3)]
            if not matches:
                continue
            _, rect, color_name = min(matches, key=lambda item: item[0])
            colored_words.append({
                "text": str(word[4]).strip(),
                "color": color_name,
                "heading": heading_for(word),
                "x": round(center[0], 1),
                "y": round(center[1], 1),
                "rect": rect,
            })

        lines = [
            "PDF-native visual evidence (vector fill colors mapped to text geometry; treat as primary for exact colors):"
        ]
        legend_groups: dict[tuple[str, str], list[str]] = {}
        for item in colored_words:
            text = item["text"]
            if not text.isdigit() and ("route" in text.lower() or len(text) <= 12):
                legend_groups.setdefault((item["color"], "legend"), []).append(text)
        if legend_groups:
            legend_text = "; ".join(
                f"{color} fill contains {' '.join(dict.fromkeys(values))}"
                for (color, _), values in legend_groups.items()
            )
            lines.append(f"Color/label candidates: {legend_text}")

        grouped: dict[tuple[str, str], list[tuple[float, float, str]]] = {}
        for item in colored_words:
            if item["text"].isdigit() or item["heading"] != "page":
                grouped.setdefault((item["heading"], item["color"]), []).append((item["y"], item["x"], item["text"]))
        for (heading, color), values in sorted(grouped.items(), key=lambda item: item[0][0]):
            ordered = [text for _, _, text in sorted(values, key=lambda row: (row[0], row[1]))]
            deduped = list(dict.fromkeys(ordered))
            if deduped:
                lines.append(f"{heading} — {color} filled text/cells: {', '.join(deduped[:80])}")

        if len(lines) == 1:
            lines.append(f"Detected {len(drawings)} colored vector regions, but no text was directly inside them.")
        return "\n".join(lines)[:12000]
    except Exception as exc:
        logging.debug("DOCLING: PDF-native visual evidence unavailable: %s", exc)
        return "PDF-native visual evidence unavailable; use the rendered page cautiously."


def _question_visual_clip(page, question: str | None):
    """Find a focused layout region near question-relevant headings."""
    try:
        terms = _question_terms(question)
        if not terms:
            return None
        words = [word for word in page.get_text("words") if len(word) >= 5]
        matches = []
        for word in words:
            text = str(word[4]).strip()
            normalized = re.sub(r"[^a-z0-9]", "", text.lower())
            if normalized in terms and len(normalized) >= 4 and not normalized.isdigit():
                matches.append((normalized in _VISUAL_QUESTION_TERMS, -len(normalized), float(word[1]), word))
        if not matches:
            return None
        # Prefer specific document headings (e.g. a month or section name) over
        # generic visual words such as "blue", "route", or "table".
        target = sorted(matches, key=lambda item: (item[0], item[1], item[2]))[0][3]
        x0, y0, x1, y1 = map(float, target[:4])
        # Include the heading and the table/grid immediately beneath it.
        clip = page.rect & page.rect.__class__(x0 - 35, y0 - 18, x1 + 80, y1 + 175)
        if clip.width < 120 or clip.height < 120:
            return None
        return clip
    except Exception:
        return None


def _ordered_page_chunks(result: dict, question: str | None = None) -> list[dict[str, object]]:
    chunks = result.get("page_chunks") or []
    if not chunks:
        preferred = result.get("markdown") or result.get("plain_text") or ""
        chunks = _page_chunks(preferred)
    normalized = [
        {"page": item.get("page", index), "text": _clean_extracted_content(item.get("text") or "")}
        for index, item in enumerate(chunks, start=1)
        if isinstance(item, dict) and _clean_extracted_content(item.get("text") or "")
    ]
    terms = _question_terms(question)
    if not terms or len(normalized) <= 1:
        return normalized

    scored = []
    for index, item in enumerate(normalized):
        words = set(re.findall(r"[a-z0-9]{3,}", str(item["text"]).lower()))
        score = len(terms & words)
        scored.append((score, index, item))
    if not any(score for score, _, _ in scored):
        return normalized
    # Keep document order among ties, but put question-relevant pages first so
    # a bounded preview does not silently omit the useful page of a long PDF.
    return [item for _, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))]


def build_extracted_text_preview(
    result: dict,
    max_len: int = 2000,
    question: str | None = None,
) -> str:
    chunks = _ordered_page_chunks(result, question)
    if not chunks:
        logging.warning(
            "⚠️ DOCLING: no extracted content available for preview source=%s status=%s",
            result.get("source_name"),
            result.get("docling_status"),
        )
        return ""
    pieces = []
    used = 0
    for chunk in chunks:
        page_text = f"[Page {chunk['page']}]\n{chunk['text']}"
        separator = "\n\n" if pieces else ""
        remaining = max_len - used - len(separator)
        if remaining <= 0:
            break
        if len(page_text) > remaining:
            page_text = safe_preview(page_text, max_len=remaining)
        pieces.append(separator + page_text)
        used += len(separator) + len(page_text)
        if used >= max_len:
            break
    preview = "".join(pieces).strip()
    logging.info(
        "📄 DOCLING: preview built source=%s preview_chars=%s pages=%s question_focused=%s",
        result.get("source_name"),
        len(preview),
        len(chunks),
        bool(_question_terms(question)),
    )
    return preview


def should_use_visual_fallback(result: dict, question: str | None = None) -> bool:
    """Decide when rendered page inspection can add information text extraction misses."""
    if str(result.get("source_type") or "").lower() != "pdf":
        return False
    terms = _question_terms(question)
    visual_question = bool(terms & _VISUAL_QUESTION_TERMS)
    # Embedded images alone do not justify a slow vision call for every PDF.
    # Run it automatically for a visual question, or when text extraction is
    # genuinely weak/partial. This keeps normal document turns fast while
    # preserving the hard cases (colors, maps, shaded cells, layouts).
    low_quality = (
        int(result.get("text_chars") or 0) < 400
        or bool(result.get("errors"))
        or str(result.get("docling_status") or "") != "success"
    )
    return bool(visual_question or low_quality)


async def analyze_pdf_visual_fallback(
    file_bytes: bytes,
    result: dict,
    question: str | None = None,
    max_pages: int = 4,
) -> list[dict[str, object]]:
    """Render a few PDF pages and ask Emery's vision model for focused facts.

    This is deliberately a fallback, not a second document parser. It gives the
    text-only main model a grounded description of colors, maps, charts, and
    layout that Docling's text export cannot represent.
    """
    if not file_bytes or not should_use_visual_fallback(result, question):
        return []
    try:
        import pymupdf as fitz
        from emery.helpers import compress_image_bytes, get_image_description

        document = fitz.open(stream=file_bytes, filetype="pdf")
        if document.page_count == 0:
            return []
        analyses = []
        async with asyncio.timeout(VISUAL_FALLBACK_MAX_SECONDS):
            for page_index in range(min(max_pages, document.page_count)):
                page = document.load_page(page_index)
                native_facts = _pdf_native_visual_facts(page, question)
                focus_clip = _question_visual_clip(page, question)
                render_clip = focus_clip or page.rect
                visual_prompt = (
                    "Inspect this rendered PDF region as a verification pass. Use the PDF-native evidence below "
                    "as primary for exact colors and text-cell associations. Use the image to validate layout, "
                    "legends, table boundaries, and any details the native extraction cannot represent. "
                    "Do not infer a color's meaning from the user's wording. Do not guess. Return concise, "
                    "structured observations and explicitly flag conflicts or illegible details.\n\n"
                    f"User focus: {str(question or 'Identify important visual information on this page.').strip()}\n\n"
                    f"{native_facts}"
                )
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(VISUAL_RENDER_SCALE, VISUAL_RENDER_SCALE),
                    clip=render_clip,
                    alpha=False,
                )
                image_bytes = pixmap.tobytes("jpg")
                image_bytes = compress_image_bytes(
                    image_bytes,
                    max_dim=VISUAL_RENDER_MAX_DIM,
                    quality=VISUAL_RENDER_JPEG_QUALITY,
                )
                try:
                    description = await asyncio.wait_for(
                        get_image_description(
                            base64.b64encode(image_bytes).decode("ascii"),
                            visual_prompt,
                            think=False,
                            num_ctx=4096,
                            priority="user",
                        ),
                        timeout=VISUAL_FALLBACK_PAGE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logging.warning("⚠️ DOCLING: visual analysis timed out on PDF page %s.", page_index + 1)
                    description = "Vision verification timed out; PDF-native evidence is retained."
                analyses.append({
                    "page": page_index + 1,
                    "description": str(description or "").strip(),
                    "native_facts": native_facts,
                    "focus_region": "question-matched" if focus_clip else "full-page",
                })
        document.close()
        return analyses
    except ImportError:
        logging.warning("⚠️ DOCLING: PyMuPDF is unavailable; visual PDF fallback is disabled.")
    except asyncio.TimeoutError:
        logging.warning("⚠️ DOCLING: visual PDF fallback reached its %s-second budget.", VISUAL_FALLBACK_MAX_SECONDS)
    except Exception as exc:
        logging.warning("⚠️ DOCLING: visual PDF fallback failed for %s: %s", result.get("source_name"), exc)
    return []


def build_document_context_text(
    *,
    source_name: str,
    source_type: str,
    mime_type: str | None = None,
    caption: str | None = None,
    docling_status: str | None = None,
    extracted_preview: str | None = None,
    error: str | None = None,
    page_count: int | None = None,
    visual_analysis: list[dict[str, object]] | None = None,
    origin_label: str = "sent a document.",
) -> str:
    lines = [origin_label]
    lines.append(f"Document Name: {source_name}")
    lines.append(f"Document Type: {source_type}")
    if mime_type:
        lines.append(f"MIME Type: {mime_type}")
    if caption:
        lines.append(f"Caption: {caption}")
    if docling_status:
        lines.append(f"Docling Status: {docling_status}")
    if page_count:
        lines.append(f"Document Pages: {page_count}")
    if error:
        lines.append(f"Docling Note: {error}")
    if extracted_preview:
        lines.append(f"Extracted Text Preview: {extracted_preview}")
    if visual_analysis:
        lines.append(
            "Visual Verification Evidence (supplemental; PDF-native geometry/color facts take precedence over "
            "the vision description, and conflicts must be reported):"
        )
        for item in visual_analysis:
            lines.append(f"[Page {item.get('page')} | region={item.get('focus_region', 'full-page')}]")
            if item.get("native_facts"):
                lines.append(str(item.get("native_facts")))
            if item.get("description"):
                lines.append(f"Vision description (secondary): {item.get('description')}")
    return "\n".join(lines)
