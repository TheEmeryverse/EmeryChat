# Qwen Image runtime handoff

The EmeryChat image path runs on the Linux B580 host:

* ComfyUI runtime broker: `http://127.0.0.1:8188` (public host address `192.168.1.121:8188`)
* Qwen3-VL conditioning service: `http://127.0.0.1:8086`
* Qwen Image 2.1 model and VAE: loaded by ComfyUI on the Intel Arc B580

The custom node is `client/remote_qwen_image21.py`, linked from
`software/ComfyUI/custom_nodes/remote_qwen_image21.py`. It handles schema 1
text-to-image requests and schema 2 photo-edit requests. Schema 2 sends one
resized PNG reference image to the authenticated encoder; the node adds the
local VAE reference latent to positive and negative conditioning.

EmeryChat uploads edit inputs through the runtime broker's `/upload/image`
route. The broker starts ComfyUI on the B580, proxies the upload, then reuses
the runtime for the edit prompt. The app workflow dynamically adds `LoadImage`
and connects it to the remote encoder and the Qwen VAE.

The Linux encoder uses the systemd user service
`emerychat-qwen-encoder-gpu.service`; its bearer token stays in the host's
private service environment file. The runtime broker uses
`emerychat-qwen-b580-runtime.service` and restores Ornith after the image queue
drains.
