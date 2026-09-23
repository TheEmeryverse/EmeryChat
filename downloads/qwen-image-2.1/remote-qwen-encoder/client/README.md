# B580 ComfyUI client node

The installed node is linked at `ComfyUI/custom_nodes/remote_qwen_image21.py`; the link targets this file. The node supports schema-1 text-to-image and schema-2 single-reference-image edit workflows.

Configure:

* `server_url`: `http://127.0.0.1:8086`
* `auth_token`: the same value as Linux `REMOTE_QWEN_TOKEN`
* `prompt` and `negative_prompt`: the Qwen Image prompts

Connect `positive` and `negative` to the Qwen Image sampler. Do not also load another Qwen text encoder. For edits, connect `LoadImage` to the node's optional `image` input and the Qwen VAE to its optional `vae` input. EmeryChat resizes the reference image to roughly 1920x1080 pixels of area while preserving its aspect ratio for conditioning, and uses a 1920x1080 landscape or 1080x1920 portrait output latent based on the source orientation.
