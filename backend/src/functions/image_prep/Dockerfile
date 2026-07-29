# Container image for the image_prep Lambda.
#
# This is the only container function in the stack. It exists because the
# generative restoration stack (torch + Real-ESRGAN + GFPGAN) is roughly 4GB
# installed, far past the 250MB Lambda layer limit. Everything else stays as a
# zip function on the shared dependencies layer.
#
# ARCHITECTURE: arm64. Lambda runs this on Graviton2 (~20% cheaper) and it
# builds natively on an Apple Silicon Mac. Building linux/amd64 here would mean
# QEMU emulation and a build measured in tens of minutes.
#
# Build context is backend/src (see DockerContext in template.yaml) so that
# shared/ can be copied alongside the function.

FROM public.ecr.aws/lambda/python:3.12-arm64

# OpenCV needs libGL and glib at runtime even in the headless build.
RUN dnf install -y mesa-libGL glib2 && dnf clean all

# ── Python dependencies ──────────────────────────────────────────────────────
# torch goes in its own layer, first: basicsr's setup.py imports torch, so
# installing them together fails. A separate layer also means dependency
# changes below don't force a 2GB torch re-download on every rebuild.
COPY functions/image_prep/requirements.txt /tmp/requirements.txt

# The cache purge must happen in THIS layer, not a later one — a deleted file in
# a later layer still occupies space in the one that created it. basicsr,
# gfpgan and realesrgan build from sdists, and those builds leave ~2.7GB in
# /root/.cache that --no-cache-dir does not cover.
RUN pip install --no-cache-dir \
        torch==2.2.2 torchvision==0.17.2 \
        --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -rf /root/.cache /tmp/pip-* /tmp/requirements.txt

# basicsr 1.4.2 imports `torchvision.transforms.functional_tensor`, removed in
# torchvision 0.17. The function it wants lives in `.functional` now and is
# API-compatible. Patch rather than pin torchvision back, because torch 2.2 is
# the floor for Python 3.12 support and it requires torchvision >= 0.17.
#
# The file is located via `site` rather than by importing basicsr, because
# importing basicsr is precisely what this patch fixes — basicsr/__init__ pulls
# in data/degradations.py, so the broken import fires before any patch code
# could run. A heredoc will not work here either: the classic Docker builder
# does not support them and silently treats the block as an empty command.
RUN SP=$(python -c "import site; print(site.getsitepackages()[0])") \
 && sed -i 's/torchvision\.transforms\.functional_tensor/torchvision.transforms.functional/' \
      "$SP/basicsr/data/degradations.py" \
 && python -c "import basicsr, gfpgan, realesrgan; print('basicsr import OK')"

# ── Model weights ────────────────────────────────────────────────────────────
# Baked into the image rather than fetched at runtime. GFPGAN and facexlib will
# otherwise lazily download ~400MB from GitHub on every cold start, which is
# both slow and an availability dependency on github.com.
#
# facexlib resolves its weights relative to its own package directory, and
# gfpgan does the same, so the files have to land inside site-packages.
ENV MODEL_DIR=/opt/ml/models
RUN mkdir -p ${MODEL_DIR} \
 && curl -fsSL -o ${MODEL_DIR}/GFPGANv1.4.pth \
      https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth \
 && curl -fsSL -o ${MODEL_DIR}/RealESRGAN_x4plus.pth \
      https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth

# The face detection and parsing weights are fetched by GFPGAN's
# FaceRestoreHelper, which resolves the relative path 'gfpgan/weights' against
# the PROCESS WORKING DIRECTORY — /var/task on Lambda, not the installed
# package. That directory is READ-ONLY at runtime, so if the files are not
# already there the download fails and every restoration fails with it. This
# works fine in a local container and breaks only in production, so the files
# are placed at the exact path the library looks in.
#
# parsing_parsenet ships under v0.2.2 while detection_Resnet50_Final ships
# under v0.1.0 — the two come from different facexlib releases.
#
# Downloaded once and symlinked rather than copied: three locations of the same
# 185MB pair is 370MB of image for nothing.
RUN mkdir -p ${MODEL_DIR} \
 && curl -fsSL -o ${MODEL_DIR}/detection_Resnet50_Final.pth \
      https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth \
 && curl -fsSL -o ${MODEL_DIR}/parsing_parsenet.pth \
      https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth \
 && FACEXLIB_W=$(python -c "import facexlib,os;print(os.path.join(os.path.dirname(facexlib.__file__),'weights'))") \
 && mkdir -p "$FACEXLIB_W" ${LAMBDA_TASK_ROOT}/gfpgan/weights \
 && for w in detection_Resnet50_Final parsing_parsenet; do \
      ln -sf ${MODEL_DIR}/$w.pth "$FACEXLIB_W/$w.pth"; \
      ln -sf ${MODEL_DIR}/$w.pth ${LAMBDA_TASK_ROOT}/gfpgan/weights/$w.pth; \
    done

# torch spawns one thread per core by default and then thrashes on Lambda's
# constrained scheduler. Pinned at runtime in the handler too; set here so any
# import-time work is also bounded.
ENV OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    PYTHONUNBUFFERED=1

# ── Application code ─────────────────────────────────────────────────────────
COPY shared/ ${LAMBDA_TASK_ROOT}/shared/
COPY functions/image_prep/ ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
