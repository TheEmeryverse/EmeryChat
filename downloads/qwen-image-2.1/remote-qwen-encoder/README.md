# Remote Qwen Image 2.1 encoder

This is the Linux-side preparation for the distributed setup:

* Ornith remains on the Intel B580.
* This process loads the configured Qwen3-VL encoder on the host accelerator and listens on port `8086`.
* The image generator remains on the Intel Arc B580 through ComfyUI.
* The B580 ComfyUI runtime calls the versioned encode endpoint locally.

The service accepts protocol schema 1 for text-to-image and schema 2 for image editing with one reference image. Schema 2 carries an `input_image` PNG as base64 plus a `keep_vision` flag; it returns the positive and negative Qwen conditioning objects with image-token conditioning. ComfyUI adds the VAE reference latent locally.

## Environment

```text
COMFYUI_ROOT=/absolute/path/to/ComfyUI
QWEN_ENCODER_PATH=/absolute/path/to/qwen3vl_8b_int8_convrot.safetensors
REMOTE_QWEN_HOST=0.0.0.0
REMOTE_QWEN_PORT=8086
REMOTE_QWEN_TOKEN=replace-with-a-long-random-token
REMOTE_QWEN_CPU_THREADS=12
```

## API

```text
GET  /health
POST /v1/qwen-image-2.1/encode
Authorization: Bearer <REMOTE_QWEN_TOKEN>
Content-Type: application/json
```

Request:

```json
{"schema":1,"prompt":"a red fox in snow","negative_prompt":"text, watermark"}
```

For image editing, send schema 2:

```json
{"schema":2,"prompt":"Replace the cloudy sky with a sunset","negative_prompt":"text, watermark","input_image":"<base64 PNG>","keep_vision":false}
```

The response contains a base64 `torch.save` payload with the two ComfyUI conditioning objects. The Mac custom node adds local VAE reference latents before sampling. The client must only deserialize responses from this authenticated service.

## Current status

On the Linux host, the service runs as the
`emerychat-qwen-encoder-gpu.service` systemd user service, bound to
`0.0.0.0:8086`, with the GTX 1070 selected as `cuda:0`. It keeps the encoder
resident in GPU memory. The bearer token is stored outside the repository in
`/home/hudson/.config/emerychat/remote-qwen-encoder.env`.
