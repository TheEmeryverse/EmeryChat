"""ComfyUI client node for the remote Qwen Image 2.1 text/image encoder."""

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request

import comfy.utils
import node_helpers
import torch


class RemoteQwenImage21TextEncode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
                "negative_prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
                "server_url": ("STRING", {"default": "http://linux-host:8086"}),
            },
            "optional": {
                "auth_token": ("STRING", {"default": "", "multiline": False}),
                "timeout_seconds": ("INT", {"default": 300, "min": 5, "max": 3600}),
                "image": ("IMAGE",),
                "vae": ("VAE",),
            },
        }

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING")
    RETURN_NAMES = ("positive", "negative")
    FUNCTION = "encode"
    CATEGORY = "conditioning/qwen image/remote"

    def encode(self, prompt, negative_prompt, server_url, auth_token="", timeout_seconds=300, image=None, vae=None):
        token = auth_token or os.environ.get("REMOTE_QWEN_TOKEN", "")
        url = server_url.rstrip("/") + "/v1/qwen-image-2.1/encode"
        schema = 2 if image is not None else 1
        body_data = {
            "schema": schema,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
        }
        resized = None
        if image is not None:
            import numpy as np
            from PIL import Image

            samples = image[:1].movedim(-1, 1)
            ratio = samples.shape[3] / samples.shape[2]
            total_pixels_side = 1024
            width = max(32, round((total_pixels_side**2 * ratio) ** 0.5 / 32) * 32)
            height = max(32, round((total_pixels_side**2 / ratio) ** 0.5 / 32) * 32)
            resized = comfy.utils.common_upscale(samples, width, height, "lanczos", "disabled")
            pixels = (resized[0].movedim(0, -1)[:, :, :3].clamp(0, 1).cpu().numpy() * 255).round().astype(np.uint8)
            buffer = io.BytesIO()
            Image.fromarray(pixels).save(buffer, format="PNG")
            body_data["input_image"] = base64.b64encode(buffer.getvalue()).decode("ascii")
            body_data["keep_vision"] = vae is None
        body = json.dumps(
            body_data,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    result = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Remote Qwen encoder HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 2:
                    raise RuntimeError(f"Remote Qwen encoder unavailable at {url}: {exc}") from exc
                time.sleep(1.0 * (attempt + 1))

        if result.get("schema") != schema or result.get("encoding") != "torch.save/base64":
            raise RuntimeError("Remote Qwen encoder returned an unsupported response schema")

        payload = base64.b64decode(result["payload"])
        try:
            archive = torch.load(io.BytesIO(payload), map_location="cpu", weights_only=True)
        except TypeError:  # compatibility with older PyTorch releases
            archive = torch.load(io.BytesIO(payload), map_location="cpu")
        if archive.get("schema") != schema:
            raise RuntimeError("Remote Qwen conditioning archive has an unsupported schema")
        positive, negative = archive["positive"], archive["negative"]
        if resized is not None and vae is not None:
            reference_latent = vae.encode(resized.movedim(1, -1))
            reference_values = {"reference_latents": [reference_latent]}
            positive = node_helpers.conditioning_set_values(positive, reference_values, append=True)
            negative = node_helpers.conditioning_set_values(negative, reference_values, append=True)
        return (positive, negative)


NODE_CLASS_MAPPINGS = {"RemoteQwenImage21TextEncode": RemoteQwenImage21TextEncode}
NODE_DISPLAY_NAME_MAPPINGS = {
    "RemoteQwenImage21TextEncode": "Remote Qwen Image 2.1 Text Encode",
}
