"""
Illustrations: one image-model call per picture, no chat model in the loop.

The lesson writer already wrote a `brief` for the picture, so the prompt is that brief wrapped in a fixed
house style (which keeps every picture in a lesson in the same look) and the age rules. The image comes
back as base64, is shrunk to WebP and returned as a data URL, which is stored in the lesson like any
other visual's code.

Settings (same sources as the other model settings: app Settings, else the environment):
  ILLUSTRATIONS_ENABLED     off by default
  ILLUSTRATION_MODEL        an OpenRouter image model
  ILLUSTRATIONS_PER_LESSON  cap, since an image costs ~30x a code visual

Run:  python -m uv run python -m learning_service.visuals.illustrate "a seed sprouting in soil" --age 10
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import os
import time
from functools import lru_cache
from pathlib import Path

import httpx
from PIL import Image

from app.generation import model_settings

log = logging.getLogger("visuals")

ENDPOINT = "https://openrouter.ai/api/v1/images"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
DEFAULT_PER_LESSON = 2
ASPECT_RATIO = "4:3"
RESOLUTION = "1K"
MAX_WIDTH = 1024  # wider than any lesson column; keeps the stored image small
WEBP_QUALITY = 80

# (max age, style) - first band whose max age >= the learner's age wins
STYLES = [
    (
        11,
        "A children's picture-book illustration in a clean flat vector style: bold simple shapes, soft "
        "rounded forms, a warm friendly palette, gentle even lighting.",
    ),
    (
        99,
        "A modern science-textbook illustration: semi-flat vector style with subtle shading, accurate "
        "proportions and realistic colours, clean confident linework.",
    ),
]
COMPOSITION = (
    "One clear subject in the centre, uncluttered, on a plain light background, with every important part "
    "fully in view."
)
NO_TEXT = (
    "Absolutely no text, letters, numbers, labels, captions, arrows with words, watermarks or logos anywhere "
    "in the image."
)


class IllustrationFailed(RuntimeError):
    """The image model gave nothing usable."""


def _setting(name: str, default):
    runtime = model_settings.get()
    if runtime is not None:
        return getattr(runtime, name, default)
    raw = os.getenv(name.upper())
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return type(default)(raw)


def enabled() -> bool:
    return _setting("illustrations_enabled", False)


def per_lesson() -> int:
    """How many illustrations a lesson may have; 0 when they are switched off."""
    return _setting("illustrations_per_lesson", DEFAULT_PER_LESSON) if enabled() else 0


def model() -> str:
    return _setting("illustration_model", DEFAULT_MODEL)


def prompt(brief: str, topic: str, age: int) -> str:
    style = next(s for max_age, s in STYLES if age <= max_age)
    return (
        f"{style} {COMPOSITION}\n"
        f"Show: {brief.strip()}\n"
        f"It illustrates a lesson on {topic.strip()} for a {age}-year-old, so it must be accurate and "
        "friendly: nothing scary, violent, gory or suggestive.\n"
        f"{NO_TEXT}"
    )


def _timeout() -> float:
    return float(_setting("model_timeout_seconds", 60))


def to_webp(data: bytes) -> bytes:
    """Any image the model returns, as a WebP no wider than MAX_WIDTH."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise IllustrationFailed("the image model returned something that isn't an image") from exc
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
    return out.getvalue()


def _image_bytes(item: dict) -> bytes:
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    url = item.get("url") or ""
    if url.startswith("data:image/") and ";base64," in url:
        return base64.b64decode(url.split(",", 1)[1])
    raise IllustrationFailed("the image model returned no image data")


@lru_cache(maxsize=64)  # same picture asked for again (a redraw, a repeated topic): no second call
def _generate(model_id: str, text: str) -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise IllustrationFailed("OPENROUTER_API_KEY is not set")
    started = time.monotonic()
    try:
        response = httpx.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model_id,
                "prompt": text,
                "aspect_ratio": ASPECT_RATIO,
                "resolution": RESOLUTION,
            },
            timeout=_timeout(),
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IllustrationFailed(f"image request failed ({type(exc).__name__})") from exc
    items = body.get("data") or []
    if not items:
        raise IllustrationFailed("the image model returned no image")
    webp = to_webp(_image_bytes(items[0]))
    log.info(
        "illustration by %s in %.1fs, $%s, %d KB",
        model_id,
        time.monotonic() - started,
        (body.get("usage") or {}).get("cost", "?"),
        len(webp) // 1024,
    )
    return "data:image/webp;base64," + base64.b64encode(webp).decode("ascii")


def illustrate(brief: str, topic: str, age: int) -> str:
    """A data URL (WebP) for the picture the brief describes. Raises IllustrationFailed."""
    return _generate(model(), prompt(brief, topic, age))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("--age", type=int, required=True)
    ap.add_argument("--topic", default="science")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="illustration.webp")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    url = _generate(args.model or model(), prompt(args.brief, args.topic, args.age))
    Path(args.out).write_bytes(base64.b64decode(url.split(",", 1)[1]))
    print(f"-> {args.out}")
