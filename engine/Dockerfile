# syntax=docker/dockerfile:1
# Recording + edit image for demo videos: Playwright's Chromium (pinned with the npm package),
# ffmpeg, Python via uv. Build context = this engine folder.
#   docker build -t dvm-engine engine
FROM mcr.microsoft.com/playwright:v1.59.1-noble@sha256:b0ab6f3cb99aa7803adbc14d9027ec1785fc6e433b97e134e0f8fe61683b6b53

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/work/.venv \
    UV_LINK_MODE=copy
WORKDIR /work

# ffmpeg for encode/mux, system Python 3.12 for uv, fonts for the card/overlay fallbacks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg python3 python3-venv fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6.2@sha256:01ddc2a91588f1210396433c79c9f58848ad668ea05bda895f5a1a31f2e5b64f /uv /usr/local/bin/uv

# Dependency layers first so source edits keep the install cache
COPY package.json package-lock.json ./
RUN npm ci
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY playwright.config.ts ./
COPY lib ./lib

# Videos are mounted at run time: -v "$PWD/00-smoke:/work/00-smoke" -e VIDEO=00-smoke
ENV PATH="/work/.venv/bin:${PATH}"
CMD ["bash"]
