# ComfyUI workflow handoff

Place the B580 ComfyUI **Save (API Format)** export here as
`qwen-image-2.1-api.json` after the workflow has been tested interactively.

The API workflow must contain `RemoteQwenImage21TextEncode`, connect its
positive and negative outputs to the Qwen Image sampler, and end in a
`SaveImage` node. Do not include a local Qwen `CLIPLoader` or
`CLIPTextEncode`; the Qwen3-VL encoder runs on the GTX 1070 at
`http://127.0.0.1:8086`, while the image model runs on the B580.

The default negative prompt in the workflow targets common quality, anatomy,
skin-rendering, and image-artifact problems while leaving intentional art
styles available. It can be overridden by editing the workflow's
`negative_prompt` input.

For `/image-edit`, EmeryChat uploads the Telegram photo to the B580 runtime,
adds a `LoadImage` node, and connects it to `RemoteQwenImage21TextEncode`. The
custom node must support its optional `image` and `vae` inputs, and the Qwen
VAE must be connected so the node can attach the reference latent to its
conditioning. The runtime removes the temporary uploaded source image after
the edit finishes or fails. Edit outputs use 1920x1080 for landscape sources
and 1080x1920 for portrait sources; the reference is resized to roughly the
same 1024x1024-pixel area while preserving its original aspect ratio for
conditioning on the GTX 1070 encoder.

EmeryChat consumes this file inside the container at:

```text
/app/config/comfyui/qwen-image-2.1-api.json
```
