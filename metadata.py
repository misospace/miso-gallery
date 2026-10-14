"""Generation metadata extracted from PNG text chunks.

Automated image generators (A1111/Forge, ComfyUI) embed their prompts and
sampler settings in the PNG ``tEXt``/``iTXt`` chunks. This module reads those
chunks through Pillow and normalises them into a flat dict the gallery UI can
render lazily, one image at a time.

Two body sources are supported, in precedence order:

- ``parameters``  — the A1111/Forge block: free-text prompt lines, an optional
  ``Negative prompt:`` line, then a final ``Key: value, Key: value`` trailer.
- ``prompt``      — the ComfyUI API-format workflow JSON, used as a fallback
  when no A1111 block is present.

Three ``miso:``-prefixed keys are ours and always win over the body source:
``miso:category``, ``miso:rolls``, ``miso:recipe``.

Every value here comes from an untrusted user-supplied file, so nothing in this
module raises: :func:`read_image_metadata` returns ``None` whenever a file
cannot be read or parsed. Callers therefore never need a try/except.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)

# Hard caps. A single prompt chunk can be megabytes long; these keep one hostile
# file from bloating a JSON response or a DOM node.
MAX_PROMPT_CHARS = 4000
MAX_RECIPE_CHARS = 2000
MAX_ROLLS = 50

_BODY_FIELDS = ("positive", "negative", "seed", "model", "sampler", "steps", "cfg", "size")

# A1111 writes the negative prompt on a line of its own (older builds put the
# text on the following line, which we also accept).
_NEGATIVE_MARKER_RE = re.compile(r"^negative\s*prompt\s*:", re.IGNORECASE)

# A1111 trailer keys -> normalised field name. Compared case-insensitively.
_TRAILER_KEYS = {
    "steps": "steps",
    "sampler": "sampler",
    "cfg scale": "cfg",
    "seed": "seed",
    "size": "size",
    "model": "model",
}

# ComfyUI node classes whose inputs carry the sampler settings / checkpoint name.
_SAMPLER_CLASSES = {"KSampler", "KSamplerAdvanced", "SamplerCustom"}
_LOADER_CLASSES = {"CheckpointLoaderSimple", "CheckpointLoader", "UNETLoader"}
_TEXT_ENCODE_CLASS = "CLIPTextEncode"

_SAMPLER_INPUT_FIELDS = (("seed", "seed"), ("sampler_name", "sampler"), ("steps", "steps"), ("cfg", "cfg"))


def read_image_metadata(path: Path) -> dict | None:
    """Return generation metadata for a PNG, or ``None`` when there is none.

    The returned dict always carries the same keys: ``positive``, ``negative``,
    ``seed``, ``model``, ``sampler``, ``steps``, ``cfg``, ``size``, ``category``
    and ``recipe`` (each ``str | None``) plus ``rolls`` (``list[str]``). This
    function never raises — a truncated, corrupt, hostile or simply metadata-free
    PNG all yield ``None``.
    """
    try:
        return _read_metadata(Path(path))
    except Exception:  # noqa: BLE001 - untrusted input, must never surface a 500
        log.debug("metadata read failed for %s", path, exc_info=True)
        return None


def _read_metadata(path: Path) -> dict | None:
    # Only PNG carries these chunks. Short-circuiting on the suffix keeps the
    # JPEG/WebP/video paths in the gallery from paying for an Image.open().
    if path.suffix.lower() != ".png":
        return None

    try:
        with Image.open(path) as image:
            # Touching .text makes Pillow decode the file, so keep the access
            # inside the guarded block.
            chunks = image.text
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        # UnidentifiedImageError subclasses OSError; SyntaxError covers the
        # truncated-stream parse failures. DecompressionBombError derives
        # straight from Exception, so it has to be named explicitly or a hostile
        # header escapes this guard and falls through to the wrapper.
        return None

    if not chunks:
        return None

    metadata = _empty_metadata()
    _apply_body(metadata, chunks)
    _apply_miso_keys(metadata, chunks)
    # A PNG can carry unrelated chunks (Software, Copyright) with nothing to do
    # with generation. Report None there so the caller hides the section rather
    # than rendering an empty box.
    if not any(value for value in metadata.values()):
        return None
    return metadata


def _empty_metadata() -> dict:
    fields: dict = {field: None for field in _BODY_FIELDS}
    fields.update({"category": None, "recipe": None, "rolls": []})
    return fields


def _chunk_text(chunks: dict, key: str) -> str | None:
    """Return a chunk value as a plain string, or ``None`` if absent/blank.

    Pillow hands back ``iTXt`` for international chunks; that class subclasses
    ``str``, so ``str(value)`` normalises both chunk flavours.
    """
    value = chunks.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _apply_body(metadata: dict, chunks: dict) -> None:
    """Fill the generation fields from ``parameters``, else from ``prompt``."""
    parameters = _chunk_text(chunks, "parameters")
    if parameters is not None:
        metadata.update(_parse_parameters(parameters))
        return
    workflow = _chunk_text(chunks, "prompt")
    if workflow is not None:
        metadata.update(_parse_comfy_prompt(workflow))


def _apply_miso_keys(metadata: dict, chunks: dict) -> None:
    """Apply our own ``miso:`` chunks, which override the body source."""
    metadata["category"] = _chunk_text(chunks, "miso:category")

    recipe = _chunk_text(chunks, "miso:recipe")
    metadata["recipe"] = _truncate(recipe, MAX_RECIPE_CHARS) if recipe else None

    rolls = _chunk_text(chunks, "miso:rolls")
    metadata["rolls"] = _parse_rolls(rolls) if rolls else []


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _parse_parameters(raw: str) -> dict:
    """Parse an A1111/Forge ``parameters`` block.

    Layout::

        positive line one
        positive line two
        Negative prompt: bad hands, extra fingers
        Steps: 25, Sampler: er_sde, CFG scale: 4.0, Seed: 1, Size: 1024x1536

    The trailing ``Key: value, ...`` line is parsed independently of the
    prompts, so a trailer we cannot make sense of costs the sampler fields but
    never the prompt text.
    """
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()

    # The final line is the metadata trailer in every A1111 build, but a
    # hand-written or truncated chunk may have none at all. Only claim that line
    # when it actually carries a recognised key, so a missing trailer costs the
    # sampler fields rather than the last line of the prompt.
    trailer_fields = _parse_trailer(lines[-1]) if lines else {}
    if trailer_fields:
        lines.pop()

    positive_lines: list[str] = []
    negative_lines: list[str] = []
    after_marker = False

    for line in lines:
        marker = _NEGATIVE_MARKER_RE.match(line.strip()) if not after_marker else None
        if marker is not None:
            after_marker = True
            remainder = line.strip()[marker.end():].strip()
            if remainder:
                negative_lines.append(remainder)
            continue
        (negative_lines if after_marker else positive_lines).append(line)

    return trailer_fields | {
        "positive": _clean_text("\n".join(positive_lines)),
        "negative": _clean_text("\n".join(negative_lines)),
    }


def _parse_trailer(trailer: str) -> dict:
    """Parse the A1111 ``Key: value, Key: value`` metadata line."""
    fields: dict[str, str | None] = {}
    for part in trailer.split(", "):
        key, sep, value = part.partition(": ")
        if not sep:
            continue
        name = _TRAILER_KEYS.get(key.strip().lower())
        if name and value.strip():
            fields[name] = value.strip()
    return fields


def _parse_comfy_prompt(raw: str) -> dict:
    """Parse a ComfyUI API-format workflow stored in the ``prompt`` chunk.

    Malformed JSON yields nothing rather than raising: the file is untrusted
    input, and a PNG with an unreadable workflow is still a valid PNG.
    """
    fields = _empty_metadata()
    try:
        workflow = json.loads(raw)
    except (ValueError, TypeError):
        return fields
    if not isinstance(workflow, dict):
        return fields

    nodes = {
        str(node_id): node
        for node_id, node in workflow.items()
        if isinstance(node, dict)
    }

    negative_id = None
    for node in nodes.values():
        if node.get("class_type") not in _SAMPLER_CLASSES:
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            for chunk_key, field in _SAMPLER_INPUT_FIELDS:
                value = inputs.get(chunk_key)
                if value is not None:
                    fields[field] = str(value)
            negative_id, negative_text = _comfy_negative(nodes, inputs.get("negative"))
            fields["negative"] = _clean_text(negative_text) if negative_text else None
        break

    # The negative prompt is itself a CLIPTextEncode node, so it has to be
    # excluded when picking the positive one — dict order is the workflow's.
    for node_id, node in nodes.items():
        if node_id == negative_id or node.get("class_type") != _TEXT_ENCODE_CLASS:
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict) and isinstance(inputs.get("text"), str):
            fields["positive"] = _clean_text(inputs["text"])
        break

    for node in nodes.values():
        if node.get("class_type") not in _LOADER_CLASSES:
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            for key in ("ckpt_name", "unet_name"):
                value = inputs.get(key)
                if isinstance(value, str) and value:
                    fields["model"] = value
                    break
        break

    return fields


def _comfy_negative(nodes: dict, link: object) -> tuple[str | None, str | None]:
    """Resolve a sampler's ``negative`` link to (node_id, prompt text).

    The link is a ``[node_id, slot]`` pair; anything else — or a link to a node
    that is not a CLIPTextEncode — yields no negative prompt.
    """
    if not isinstance(link, (list, tuple)) or not link:
        return None, None
    node_id = str(link[0])
    target = nodes.get(node_id)
    if not isinstance(target, dict) or target.get("class_type") != _TEXT_ENCODE_CLASS:
        return None, None
    inputs = target.get("inputs")
    if not isinstance(inputs, dict):
        return node_id, None
    text = inputs.get("text")
    return node_id, text if isinstance(text, str) else None


def _parse_rolls(raw: str) -> list[str]:
    """Parse ``miso:rolls`` from JSON, or from a newline/semicolon separated list."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None

    if isinstance(parsed, list):
        entries = [str(entry) for entry in parsed]
    elif isinstance(parsed, dict):
        entries = [f"{key}: {value}" for key, value in parsed.items()]
    else:
        entries = re.split(r"[\n;]", raw)

    cleaned = [entry.strip() for entry in entries]
    return [entry for entry in cleaned if entry][:MAX_ROLLS]


def _clean_text(text: str) -> str | None:
    text = text.strip()
    return _truncate(text, MAX_PROMPT_CHARS) or None
