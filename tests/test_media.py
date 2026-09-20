import asyncio

from PIL import Image

from emery import engine, media


def _jpeg_bytes():
    image = Image.new("RGB", (12, 8), color=(220, 180, 80))
    output = __import__("io").BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def test_main_history_keeps_only_latest_image_pixels(monkeypatch):
    monkeypatch.setattr(engine, "MAIN_MODEL_VISION", True)
    first = media.store_artifact(_jpeg_bytes(), label="first")
    second = media.store_artifact(_jpeg_bytes(), label="second")
    history = [
        {"role": "user", "content": "old image", "media_attachments": [{"artifact_id": first}]},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest image", "media_attachments": [{"artifact_id": second}]},
    ]

    assembled = engine._build_ollama_history(history)

    assert isinstance(assembled[0]["content"], str)
    assert isinstance(assembled[2]["content"], list)
    assert any(part.get("type") == "image_url" for part in assembled[2]["content"])


def test_research_image_budget_is_hard(monkeypatch):
    monkeypatch.setattr(media, "MAX_RESEARCH_IMAGES_PER_TURN", 2)
    state = media.begin_media_turn()
    media.queue_outbound_media({"artifact_id": "one"})
    media.queue_outbound_media({"artifact_id": "two"})

    assert state.research_images_used == 2
    assert not media.can_use_research_image()
    media.clear_media_turn()


def test_model_image_attachment_budget_is_bounded_per_loop_and_turn(monkeypatch):
    monkeypatch.setattr(media, "MAX_MODEL_IMAGE_ATTACHMENTS_PER_LOOP", 1)
    monkeypatch.setattr(media, "MAX_MODEL_IMAGE_ATTACHMENTS_PER_TURN", 2)
    state = media.begin_media_turn()

    assert media.can_attach_model_image()
    media.queue_model_attachment()
    assert not media.can_attach_model_image()

    media.begin_media_reasoning_loop()
    assert media.can_attach_model_image()
    media.queue_model_attachment()
    assert not media.can_attach_model_image()

    media.begin_media_reasoning_loop()
    assert not media.can_attach_model_image()
    assert state.model_images_used == 2
    media.clear_media_turn()


def test_tool_selected_image_is_ephemeral_model_history():
    artifact_id = media.store_artifact(_jpeg_bytes(), label="selected")
    assembled = []

    appended = engine._append_model_media_from_tool_result(
        {"_model_attachment": {"artifact_id": artifact_id, "label": "selected"}},
        assembled,
    )

    assert appended is True
    assert len(assembled) == 1
    assert assembled[0]["role"] == "user"
    assert isinstance(assembled[0]["content"], list)
    assert assembled[0]["content"][0]["type"] == "text"
    assert assembled[0]["content"][1]["image_url"]["url"].startswith("data:image/")


def test_text_main_routes_tool_image_through_dedicated_vision_model(monkeypatch):
    monkeypatch.setattr(engine, "MAIN_MODEL_VISION", False)
    artifact_id = media.store_artifact(_jpeg_bytes(), label="Browser screenshot")
    calls = []

    async def describe(image_b64, prompt):
        calls.append((image_b64, prompt))
        return "The screenshot shows a model page."

    monkeypatch.setattr("emery.helpers.get_image_description", describe)
    assembled = []
    route = asyncio.run(engine._append_tool_media_for_model(
        {"_model_attachment": {"artifact_id": artifact_id, "label": "Browser screenshot"}},
        assembled,
    ))

    assert route == "dedicated_vision"
    assert len(calls) == 1
    assert assembled == [{
        "role": "user",
        "content": "[Dedicated vision model analysis of Browser screenshot]\nThe screenshot shows a model page.",
    }]


def test_image_candidates_are_metadata_only():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<html><head><meta property="og:image" content="/hero.jpg"></head>'
        '<body><figure><img src="brick.jpg" alt="Cream City brick"></figure></body></html>',
        "html.parser",
    )
    candidates = media.extract_image_candidates(soup, "https://example.org/article")

    assert [item["url"] for item in candidates] == [
        "https://example.org/hero.jpg",
        "https://example.org/brick.jpg",
    ]
    assert all("bytes" not in item for item in candidates)
