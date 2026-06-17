import asyncio
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
        "to_formats": ["md", "text"],
        "do_ocr": True,
        "force_ocr": False,
        "ocr_engine": "easyocr",
        "ocr_lang": ["en"],
        "pdf_backend": "dlparse_v2",
        "table_mode": "fast",
        "include_images": False,
        "image_export_mode": "placeholder",
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
    cleaned = re.sub(r'!\[[^\]]*\]\(data:image/[^)]*\)', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', cleaned)
    cleaned = re.sub(r'<img\b[^>]*>', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    return cleaned.strip()


def _normalize_docling_result(
    payload: dict,
    source_name: str,
    source_type: str,
) -> dict:
    document = payload.get("document") or {}
    status = str(payload.get("status") or "failure").strip().lower()
    errors = payload.get("errors") or []
    markdown = _clean_extracted_content(document.get("md_content") or "")
    plain_text = _clean_extracted_content(document.get("text_content") or "")
    success = status in {"success", "partial_success"} and bool(markdown or plain_text)

    return {
        "success": success,
        "source_name": source_name,
        "source_type": source_type,
        "docling_status": status,
        "markdown": markdown,
        "plain_text": plain_text,
        "processing_time": payload.get("processing_time"),
        "errors": [str(item) for item in errors if item],
    }


def _fallback_docling_result(source_name: str, source_type: str, error: str) -> dict:
    return {
        "success": False,
        "source_name": source_name,
        "source_type": source_type,
        "docling_status": "failure",
        "markdown": "",
        "plain_text": "",
        "processing_time": None,
        "errors": [error] if error else [],
    }


async def convert_document_bytes(
    file_bytes: bytes,
    filename: str,
    mime_type: str | None = None,
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
        "http_sources": [{"url": url}],
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


def build_extracted_text_preview(result: dict, max_len: int = 2000) -> str:
    content = _clean_extracted_content(result.get("plain_text") or result.get("markdown") or "")
    if not content:
        logging.warning(
            "⚠️ DOCLING: no extracted content available for preview source=%s status=%s",
            result.get("source_name"),
            result.get("docling_status"),
        )
        return ""
    preview = safe_preview(content, max_len=max_len)
    logging.info(
        "📄 DOCLING: preview built source=%s preview_chars=%s",
        result.get("source_name"),
        len(preview),
    )
    return preview


def build_document_context_text(
    *,
    source_name: str,
    source_type: str,
    mime_type: str | None = None,
    caption: str | None = None,
    docling_status: str | None = None,
    extracted_preview: str | None = None,
    error: str | None = None,
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
    if error:
        lines.append(f"Docling Note: {error}")
    if extracted_preview:
        lines.append(f"Extracted Text Preview: {extracted_preview}")
    return "\n".join(lines)
