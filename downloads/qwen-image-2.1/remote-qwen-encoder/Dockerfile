FROM intel/intel-extension-for-pytorch:2.7.10-xpu

WORKDIR /opt/remote-qwen-encoder

COPY remote-qwen-encoder/requirements-runtime.txt /opt/remote-qwen-encoder/requirements-runtime.txt
RUN python -m pip install --no-cache-dir -r /opt/remote-qwen-encoder/requirements-runtime.txt

# comfy-kitchen 0.2.35 eagerly imports its Triton backend.  In a CPU-only
# container Triton has no active driver and fails during import before the
# eager CPU backend can be selected.  Disable only that import; the encoder
# still uses comfy-kitchen's eager CPU quantization path.
RUN python - <<'PY'
from pathlib import Path
import site

for root in site.getsitepackages():
    init = Path(root) / "comfy_kitchen" / "__init__.py"
    if init.exists():
        text = init.read_text()
        text = text.replace(
            "from .backends import triton as _triton_backend  # noqa: F401",
            "# Triton disabled for the CPU-only remote encoder service",
        )
        init.write_text(text)
        break
else:
    raise SystemExit("comfy_kitchen package not found")
PY

COPY remote-qwen-encoder/server.py /opt/remote-qwen-encoder/server.py
COPY software/ComfyUI /opt/ComfyUI

ENV COMFYUI_ROOT=/opt/ComfyUI \
    QWEN_ENCODER_PATH=/models/text_encoders/qwen3vl_8b_int8_convrot.safetensors \
    REMOTE_QWEN_HOST=0.0.0.0 \
    REMOTE_QWEN_PORT=8086

EXPOSE 8086
ENTRYPOINT ["python", "/opt/remote-qwen-encoder/server.py"]
