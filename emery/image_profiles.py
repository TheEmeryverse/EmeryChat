"""Quality and batch limits for generated images."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImageProfile:
    name: str
    width: int
    height: int
    steps: int
    max_batch_size: int


IMAGE_PROFILES = {
    "low": ImageProfile("low", width=512, height=512, steps=10, max_batch_size=10),
    "medium": ImageProfile("medium", width=768, height=768, steps=12, max_batch_size=5),
    "high": ImageProfile("high", width=1024, height=1024, steps=20, max_batch_size=2),
}
DEFAULT_IMAGE_PROFILE = "medium"
DIRECT_IMAGE_DEFAULT_PROFILE = "low"


def get_image_profile(name: str) -> ImageProfile:
    try:
        return IMAGE_PROFILES[str(name).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown image quality profile: {name}") from exc


def image_profile_batch_limit(name: str, application_limit: int | None = None) -> int:
    limit = get_image_profile(name).max_batch_size
    return min(limit, application_limit) if application_limit is not None else limit
