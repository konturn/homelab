#!/bin/bash
# Build a YOLOv9 ONNX model for Frigate's ONNX detector (docs/frigate.md).
# Usage: build-yolov9-model.sh <size t|s|m|c|e> <img size, e.g. 320> <dest dir>
# Writes <dest dir>/yolov9-<size>-<img size>.onnx.
#
# Follows the export recipe in Frigate's docs (docs/data/object_detectors_models.yaml,
# "YOLOv9", v0.18.2): a CPU-only export in a throwaway build, nothing is run on
# the GPU. The upstream inputs are pinned (repo commit and the uv image) so a
# rebuild produces the same model.
set -euo pipefail

size=${1:?model size}
img=${2:?image size}
dest=${3:?destination directory}
# WongKinYiu/yolov9 main as of 2026-11-07
yolov9_ref=5b1ea9a8b3f0ffe4fe0e203ec6232d788bb3fcff

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cat > "$work/Dockerfile" <<'EOF'
FROM python:3.11 AS build
RUN apt-get update && apt-get install --no-install-recommends -y cmake libgl1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /bin/
WORKDIR /yolov9
ARG YOLOV9_REF
ADD https://github.com/WongKinYiu/yolov9.git#${YOLOV9_REF} .
RUN uv pip install --system -r requirements.txt
RUN uv pip install --system onnx==1.18.0 onnxruntime onnx-simplifier==0.4.* onnxscript
ARG MODEL_SIZE
ARG IMG_SIZE
ADD https://github.com/WongKinYiu/yolov9/releases/download/v0.1/yolov9-${MODEL_SIZE}-converted.pt yolov9-${MODEL_SIZE}.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-${MODEL_SIZE}.pt --imgsz ${IMG_SIZE} --simplify --include onnx
FROM scratch
ARG MODEL_SIZE
ARG IMG_SIZE
COPY --from=build /yolov9/yolov9-${MODEL_SIZE}.onnx /yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx
EOF

DOCKER_BUILDKIT=1 docker build --network host \
  --build-arg YOLOV9_REF="$yolov9_ref" --build-arg MODEL_SIZE="$size" --build-arg IMG_SIZE="$img" \
  --output "type=local,dest=$work/out" "$work"

out="$work/out/yolov9-$size-$img.onnx"
[ -s "$out" ]
mkdir -p "$dest"
install -m 0644 "$out" "$dest/yolov9-$size-$img.onnx.tmp"
mv "$dest/yolov9-$size-$img.onnx.tmp" "$dest/yolov9-$size-$img.onnx"
echo "built $dest/yolov9-$size-$img.onnx"
