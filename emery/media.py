"""Bounded media artifacts shared by the chat engine and media tools.

Images are intentionally kept out of chat history as base64.  History stores
small references and this module resolves those references only while building
the current model request or final Telegram delivery.
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image

import emery.globals as globals
from emery.config import (
    MAIN_MODEL_IMAGE_FORMAT,
    MAX_MODEL_IMAGE_ATTACHMENTS_PER_LOOP,
    MAX_MODEL_IMAGE_ATTACHMENTS_PER_TURN,
    MAX_RESEARCH_IMAGES_PER_TURN,
    RESEARCH_IMAGE_CACHE_TTL_SECONDS,
    RESEARCH_IMAGE_MAX_BYTES,
    RESEARCH_IMAGE_MAX_DIMENSION,
)


_ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MAX_CANDIDATES_PER_PAGE = 8
_MAX_CANDIDATES_PER_TURN = 12
_ARTIFACTS: dict[str, dict[str, Any]] = {}
_ARTIFACT_BYTES = 0


@dataclass
class MediaTurnState:
    """Per-model-turn media budget and deferred delivery state."""

    research_candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    outbound_media: list[dict[str, Any]] = field(default_factory=list)
    research_images_used: int = 0
    model_images_used: int = 0
    model_images_used_this_loop: int = 0


def begin_media_turn() -> MediaTurnState:
    state = MediaTurnState()
    globals.CURRENT_MEDIA_TURN.set(state)
    return state


def get_media_turn() -> MediaTurnState | None:
    return globals.CURRENT_MEDIA_TURN.get()


def clear_media_turn() -> None:
    globals.CURRENT_MEDIA_TURN.set(None)


def begin_media_reasoning_loop() -> None:
    state = get_media_turn()
    if state is not None:
        state.model_images_used_this_loop = 0


def _prune_artifacts(now: float | None = None) -> None:
    global _ARTIFACT_BYTES
    now = time.monotonic() if now is None else now
    expired = [
        key for key, item in _ARTIFACTS.items()
        if now - float(item.get("created_at", now)) > RESEARCH_IMAGE_CACHE_TTL_SECONDS
    ]
    for key in expired:
        item = _ARTIFACTS.pop(key, None)
        if item:
            _ARTIFACT_BYTES -= len(item.get("bytes") or b"")
    _ARTIFACT_BYTES = max(0, _ARTIFACT_BYTES)


def store_artifact(
    file_bytes: bytes,
    *,
    mime_type: str = "image/jpeg",
    source_url: str | None = None,
    source_page_url: str | None = None,
    label: str = "image",
) -> str:
    """Store a bounded image artifact and return a non-sensitive reference ID."""
    global _ARTIFACT_BYTES
    if not file_bytes:
        raise ValueError("Image data is empty.")
    if len(file_bytes) > RESEARCH_IMAGE_MAX_BYTES:
        raise ValueError(f"Image exceeds the {RESEARCH_IMAGE_MAX_BYTES}-byte limit.")

    _prune_artifacts()
    artifact_id = f"media_{uuid.uuid4().hex[:16]}"
    _ARTIFACTS[artifact_id] = {
        "bytes": bytes(file_bytes),
        "mime_type": mime_type.split(";", 1)[0].strip().lower() or "image/jpeg",
        "source_url": source_url or "",
        "source_page_url": source_page_url or "",
        "label": label or "image",
        "created_at": time.monotonic(),
    }
    _ARTIFACT_BYTES += len(file_bytes)

    # Keep the in-process cache bounded even if many users send media.
    max_cache_bytes = max(RESEARCH_IMAGE_MAX_BYTES * 8, 32_000_000)
    while _ARTIFACT_BYTES > max_cache_bytes and _ARTIFACTS:
        oldest_id = min(_ARTIFACTS, key=lambda key: _ARTIFACTS[key].get("created_at", 0))
        oldest = _ARTIFACTS.pop(oldest_id)
        _ARTIFACT_BYTES -= len(oldest.get("bytes") or b"")
    return artifact_id


def get_artifact(artifact_id: str) -> dict[str, Any] | None:
    _prune_artifacts()
    item = _ARTIFACTS.get(str(artifact_id or ""))
    return dict(item) if item else None


def artifact_to_model_part(artifact_id: str) -> dict[str, Any] | None:
    artifact = get_artifact(artifact_id)
    if not artifact:
        return None
    encoded = base64.b64encode(artifact["bytes"]).decode("ascii")
    data_url = f"data:{artifact['mime_type']};base64,{encoded}"
    if MAIN_MODEL_IMAGE_FORMAT == "ollama":
        return {"type": "ollama_image", "data": encoded}
    return {"type": "image_url", "image_url": {"url": data_url}}


def register_research_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state = get_media_turn()
    if state is None:
        return []
    registered = []
    remaining = max(0, _MAX_CANDIDATES_PER_TURN - len(state.research_candidates))
    for candidate in candidates[: min(_MAX_CANDIDATES_PER_PAGE, remaining)]:
        image_id = f"research_img_{uuid.uuid4().hex[:12]}"
        normalized = {
            "id": image_id,
            "url": str(candidate.get("url") or "").strip(),
            "alt": str(candidate.get("alt") or "").strip()[:500],
            "caption": str(candidate.get("caption") or "").strip()[:500],
            "source_url": str(candidate.get("source_url") or "").strip(),
        }
        if not normalized["url"] or not normalized["source_url"]:
            continue
        state.research_candidates[image_id] = normalized
        registered.append(normalized)
    return registered


def get_research_candidate(image_id: str) -> dict[str, Any] | None:
    state = get_media_turn()
    if state is None:
        return None
    return state.research_candidates.get(str(image_id or ""))


def can_use_research_image() -> bool:
    state = get_media_turn()
    return state is not None and state.research_images_used < MAX_RESEARCH_IMAGES_PER_TURN


def can_attach_model_image() -> bool:
    state = get_media_turn()
    return (
        state is not None
        and state.model_images_used < MAX_MODEL_IMAGE_ATTACHMENTS_PER_TURN
        and state.model_images_used_this_loop < MAX_MODEL_IMAGE_ATTACHMENTS_PER_LOOP
    )


def queue_outbound_media(item: dict[str, Any]) -> None:
    state = get_media_turn()
    if state is None:
        raise RuntimeError("No active media turn.")
    state.outbound_media.append(item)
    state.research_images_used += 1


def queue_model_attachment() -> None:
    state = get_media_turn()
    if state is not None:
        state.model_images_used += 1
        state.model_images_used_this_loop += 1


def take_outbound_media() -> list[dict[str, Any]]:
    state = get_media_turn()
    if state is None:
        return []
    items = list(state.outbound_media)
    state.outbound_media.clear()
    return items


def _valid_public_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


def _is_private_or_local_ip(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


async def _validate_public_url(url: str) -> None:
    if not _valid_public_url(url):
        raise ValueError("Research image URL is not a valid public HTTP(S) URL.")
    hostname = urlparse(url).hostname.strip().lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Localhost image URLs are blocked.")
    try:
        ips = [hostname]
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ips = sorted({entry[4][0] for entry in await asyncio.to_thread(
                socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM
            )})
        except Exception as exc:
            raise ValueError(f"Unable to resolve image hostname: {exc}") from exc
    if any(_is_private_or_local_ip(ip) for ip in ips):
        raise ValueError("Private, loopback, link-local, multicast, or reserved image addresses are blocked.")


def extract_image_candidates(soup, page_url: str) -> list[dict[str, Any]]:
    """Extract descriptive image metadata without downloading image bytes."""
    candidates = []
    seen = set()

    def add(url: str, *, alt: str = "", caption: str = "") -> None:
        resolved = urljoin(page_url, str(url or "").strip())
        if not _valid_public_url(resolved) or resolved in seen:
            return
        seen.add(resolved)
        candidates.append({
            "url": resolved,
            "alt": str(alt or "").strip(),
            "caption": str(caption or "").strip(),
            "source_url": page_url,
        })

    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or "").strip().lower()
        if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            add(meta.get("content"), caption=meta.get("alt", ""))
            if candidates and len(candidates) >= _MAX_CANDIDATES_PER_PAGE:
                return candidates

    for image in soup.find_all("img"):
        src = image.get("src") or image.get("data-src")
        if not src:
            srcset = str(image.get("srcset") or "").split(",")
            if srcset:
                src = srcset[-1].strip().split(" ", 1)[0]
        figure = image.find_parent("figure")
        caption = ""
        if figure:
            figcaption = figure.find("figcaption")
            caption = figcaption.get_text(" ", strip=True) if figcaption else ""
        add(src, alt=image.get("alt", ""), caption=caption)
        if len(candidates) >= _MAX_CANDIDATES_PER_PAGE:
            break
    return candidates


async def download_research_image(candidate: dict[str, Any]) -> dict[str, Any]:
    """Download, validate, and compress one previously discovered image."""
    url = str(candidate.get("url") or "").strip()
    await _validate_public_url(url)

    headers = {
        "User-Agent": "Mozilla/5.0 EmeryChat/1.0",
        "Accept": "image/jpeg,image/png,image/webp,image/gif;q=0.9,*/*;q=0.1",
    }
    current_url = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=20.0) as client:
        response = None
        for _ in range(5):
            response = await client.get(current_url, headers=headers)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location")
            if not location:
                raise ValueError("Image redirect did not include a Location header.")
            current_url = urljoin(str(response.url), location)
            await _validate_public_url(current_url)
        if response is None or response.status_code != 200:
            raise ValueError(f"Image server returned HTTP {getattr(response, 'status_code', 'unknown')}.")
        if len(response.content) > RESEARCH_IMAGE_MAX_BYTES:
            raise ValueError("Research image exceeds the configured size limit.")

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in _ALLOWED_IMAGE_TYPES:
            raise ValueError(f"Unsupported research image type: {content_type or 'unknown'}.")
        try:
            image = Image.open(io.BytesIO(response.content))
            image.load()
            if max(image.size) > RESEARCH_IMAGE_MAX_DIMENSION:
                resampling = getattr(Image, "Resampling", Image)
                image.thumbnail((RESEARCH_IMAGE_MAX_DIMENSION, RESEARCH_IMAGE_MAX_DIMENSION), resampling.LANCZOS)
            output = io.BytesIO()
            output_format = "JPEG" if content_type in {"image/jpeg", "image/gif"} else ("PNG" if content_type == "image/png" else "WEBP")
            save_kwargs = {"format": output_format}
            if output_format == "JPEG":
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                save_kwargs["quality"] = 82
                save_kwargs["optimize"] = True
            image.save(output, **save_kwargs)
            content = output.getvalue()
        except Exception as exc:
            raise ValueError(f"Image could not be decoded safely: {exc}") from exc

    artifact_id = store_artifact(
        content,
        mime_type=(
            "image/jpeg" if output_format == "JPEG"
            else "image/png" if output_format == "PNG"
            else "image/webp"
        ),
        source_url=current_url,
        source_page_url=candidate.get("source_url"),
        label=candidate.get("alt") or candidate.get("caption") or "research image",
    )
    artifact = get_artifact(artifact_id)
    artifact["id"] = artifact_id
    artifact["resolved_url"] = current_url
    artifact["candidate"] = dict(candidate)
    return artifact
