"""Candidate-only remote encoder forcing ComfyUI CPU FP32 mode."""

from __future__ import annotations

import sys
from pathlib import Path

import server


def load_comfy_clip_fp32():
    if not server.COMFYUI_ROOT.is_dir():
        raise RuntimeError(f"COMFYUI_ROOT does not exist: {server.COMFYUI_ROOT}")
    if not server.ENCODER_PATH.is_file():
        raise RuntimeError(f"QWEN_ENCODER_PATH does not exist: {server.ENCODER_PATH}")

    sys.path.insert(0, str(server.COMFYUI_ROOT))
    sys.argv = [sys.argv[0], "--cpu", "--force-fp32"]
    import torch

    threads = server.os.environ.get("REMOTE_QWEN_CPU_THREADS")
    if threads:
        torch.set_num_threads(int(threads))

    import comfy.options
    comfy.options.enable_args_parsing()
    import folder_paths
    import nodes

    folder_paths.add_model_folder_path("text_encoders", str(server.ENCODER_PATH.parent))
    server.log.info("Loading Qwen encoder on CPU from %s with forced FP32", server.ENCODER_PATH)
    clip = nodes.CLIPLoader().load_clip(
        server.ENCODER_PATH.name,
        type="qwen_image",
        device="cpu",
    )[0]
    server.log.info("Qwen encoder loaded; device is CPU and process will keep it warm")
    return clip


server.load_comfy_clip = load_comfy_clip_fp32
sys.argv[0] = "fp32_remote_qwen_encoder_server.py"
server.main()
