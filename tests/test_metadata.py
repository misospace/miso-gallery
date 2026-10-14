"""Embedded PNG generation metadata: parser, endpoint, and template wiring (#510)."""

import copy
import json
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from conftest import build_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from metadata import (  # noqa: E402
    MAX_PROMPT_CHARS,
    MAX_RECIPE_CHARS,
    MAX_ROLLS,
    _read_metadata,
    read_image_metadata,
)

_PARAMETERS_BLOCK = (
    "a serene lakeside at dawn\nhighly detailed, 8k\n"
    "Negative prompt: bad hands, extra fingers\n"
    "Steps: 25, Sampler: er_sde, CFG scale: 4.0, Seed: 326515186, "
    "Size: 1024x1536, Model: JANIMAAnima_v1029B"
)

_COMFY_WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 8675309,
            "sampler_name": "dpmpp_2m",
            "steps": 30,
            "cfg": 7.5,
            "positive": ["6", 0],
            "negative": ["7", 0],
        },
    },
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat in a hat"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, deformed"}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
}


def _write_png(path: Path, chunks: dict) -> Path:
    """Write a tiny PNG carrying the given tEXt chunks."""
    info = PngImagePlugin.PngInfo()
    for key, value in chunks.items():
        info.add_text(key, value)
    Image.new("RGB", (8, 8)).save(path, pnginfo=info)
    return path


# --- parser ---------------------------------------------------------------


def test_read_image_metadata_parses_full_parameters_block(tmp_path):
    path = _write_png(tmp_path / "a.png", {"parameters": _PARAMETERS_BLOCK})
    meta = read_image_metadata(path)
    assert meta is not None
    assert meta["positive"] == "a serene lakeside at dawn\nhighly detailed, 8k"
    assert meta["negative"] == "bad hands, extra fingers"
    assert meta["steps"] == "25"
    assert meta["sampler"] == "er_sde"
    assert meta["cfg"] == "4.0"
    assert meta["seed"] == "326515186"
    assert meta["size"] == "1024x1536"
    assert meta["model"] == "JANIMAAnima_v1029B"
    assert meta["category"] is None
    assert meta["recipe"] is None
    assert meta["rolls"] == []


def test_read_image_metadata_parameters_without_negative_marker(tmp_path):
    path = _write_png(tmp_path / "b.png", {"parameters": "just a prompt\nSteps: 12, Seed: 42"})
    meta = read_image_metadata(path)
    assert meta["positive"] == "just a prompt"
    assert meta["negative"] is None
    assert meta["steps"] == "12"
    assert meta["seed"] == "42"


def test_read_image_metadata_keeps_prompts_when_trailer_is_unparseable(tmp_path):
    """A line carrying no known trailer key is prompt text, not a trailer."""
    path = _write_png(tmp_path / "c.png", {"parameters": "keep me\nNegative prompt: drop this\ntotal nonsense trailer"})
    meta = read_image_metadata(path)
    assert meta["positive"] == "keep me"
    assert meta["negative"] == "drop this\ntotal nonsense trailer"
    assert meta["steps"] is None


def test_read_image_metadata_keeps_single_line_prompt_with_no_trailer(tmp_path):
    """A lone prompt line must survive when the chunk has no trailer at all."""
    path = _write_png(tmp_path / "c2.png", {"parameters": "the only line"})
    meta = read_image_metadata(path)
    assert meta["positive"] == "the only line"
    assert meta["negative"] is None
    assert meta["seed"] is None


def test_read_image_metadata_none_when_chunks_carry_no_generation_fields(tmp_path):
    """Unrelated chunks (Software, Copyright) must not yield an empty section."""
    path = _write_png(tmp_path / "c3.png", {"Software": "GIMP 2.10", "Copyright": "none"})
    assert read_image_metadata(path) is None


def test_read_image_metadata_truncates_comfy_prompts(tmp_path):
    """The ComfyUI path is capped like the A1111 one, so a huge chunk is bounded."""
    workflow = copy.deepcopy(_COMFY_WORKFLOW)
    workflow["6"]["inputs"]["text"] = "x" * (MAX_PROMPT_CHARS + 500)
    workflow["7"]["inputs"]["text"] = "y" * (MAX_PROMPT_CHARS + 500)
    path = _write_png(tmp_path / "c4.png", {"prompt": json.dumps(workflow)})
    meta = read_image_metadata(path)
    assert len(meta["positive"]) == MAX_PROMPT_CHARS
    assert len(meta["negative"]) == MAX_PROMPT_CHARS


def test_read_image_metadata_comfy_prompt_fallback(tmp_path):
    path = _write_png(tmp_path / "d.png", {"prompt": json.dumps(_COMFY_WORKFLOW)})
    meta = read_image_metadata(path)
    assert meta["positive"] == "a cat in a hat"
    assert meta["negative"] == "blurry, deformed"
    assert meta["seed"] == "8675309"
    assert meta["sampler"] == "dpmpp_2m"
    assert meta["steps"] == "30"
    assert meta["cfg"] == "7.5"
    assert meta["model"] == "sd_xl_base_1.0.safetensors"
    assert meta["size"] is None


def test_read_image_metadata_parameters_win_over_comfy_prompt(tmp_path):
    path = _write_png(
        tmp_path / "e.png",
        {"parameters": _PARAMETERS_BLOCK, "prompt": json.dumps(_COMFY_WORKFLOW)},
    )
    meta = read_image_metadata(path)
    assert meta["model"] == "JANIMAAnima_v1029B"
    assert meta["positive"].startswith("a serene lakeside")


def test_read_image_metadata_miso_keys_only(tmp_path):
    path = _write_png(
        tmp_path / "f.png",
        {"miso:category": "Portraits", "miso:recipe": "base + hires fixup", "miso:rolls": "seed-a;seed-b"},
    )
    meta = read_image_metadata(path)
    assert meta["category"] == "Portraits"
    assert meta["recipe"] == "base + hires fixup"
    assert meta["rolls"] == ["seed-a", "seed-b"]
    assert meta["positive"] is None
    assert meta["negative"] is None


def test_read_image_metadata_rolls_from_json_list(tmp_path):
    path = _write_png(tmp_path / "g.png", {"miso:rolls": json.dumps(["one", 2, None])})
    assert read_image_metadata(path)["rolls"] == ["one", "2", "None"]


def test_read_image_metadata_rolls_from_json_dict(tmp_path):
    path = _write_png(tmp_path / "h.png", {"miso:rolls": json.dumps({"seed": "7", "upscale": "2x"})})
    assert read_image_metadata(path)["rolls"] == ["seed: 7", "upscale: 2x"]


def test_read_image_metadata_rolls_from_newlines_and_capped(tmp_path):
    path = _write_png(tmp_path / "i.png", {"miso:rolls": "\n".join(f"roll{n}" for n in range(MAX_ROLLS + 10))})
    rolls = read_image_metadata(path)["rolls"]
    assert len(rolls) == MAX_ROLLS
    assert rolls[0] == "roll0"


def test_read_image_metadata_returns_none_for_png_without_chunks(tmp_path):
    path = tmp_path / "plain.png"
    Image.new("RGB", (8, 8)).save(path)
    assert read_image_metadata(path) is None


def test_read_image_metadata_returns_none_for_non_png_suffix(tmp_path):
    path = tmp_path / "meta.jpg"
    _write_png(path, {"parameters": _PARAMETERS_BLOCK})
    assert read_image_metadata(path) is None


def test_read_image_metadata_returns_none_for_corrupt_bytes(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff" * 64)
    assert read_image_metadata(path) is None


def test_read_image_metadata_survives_hostile_prompt_chunk(tmp_path):
    for hostile in ("{not json", "[1, 2, 3]", '"a string"', "null", '{"3": "not-a-node"}'):
        path = _write_png(tmp_path / "hostile.png", {"prompt": hostile})
        meta = read_image_metadata(path)
        assert meta is None or meta["positive"] is None


def test_read_image_metadata_survives_decompression_bomb(tmp_path):
    """A PNG whose IHDR declares a bomb-sized canvas must return None, not raise.

    PIL.Image.DecompressionBombError derives straight from Exception rather than
    OSError, so the guard around Image.open has to name it explicitly.
    ``_read_metadata`` is asserted directly because read_image_metadata's outer
    except-Exception wrapper would mask the difference at the public boundary.
    """
    source = _write_png(tmp_path / "seed.png", {"parameters": "hello"})
    raw = bytearray(source.read_bytes())
    # IHDR data spans bytes 12..28 (type + width/height/depth/...); its CRC is
    # the next 4 bytes. Pillow validates that CRC, so it has to be recomputed.
    raw[16:20] = (30000).to_bytes(4, "big")
    raw[20:24] = (30000).to_bytes(4, "big")
    raw[29:33] = zlib.crc32(bytes(raw[12:29])).to_bytes(4, "big")
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(bytes(raw))

    with pytest.raises(Image.DecompressionBombError):
        Image.open(bomb)
    assert _read_metadata(bomb) is None
    assert read_image_metadata(bomb) is None


def test_read_image_metadata_truncates_oversized_text(tmp_path):
    long_prompt = "x" * (MAX_PROMPT_CHARS * 2)
    long_recipe = "y" * (MAX_RECIPE_CHARS * 2)
    path = _write_png(
        tmp_path / "big.png",
        {"parameters": f"{long_prompt}\nSteps: 1", "miso:recipe": long_recipe},
    )
    meta = read_image_metadata(path)
    assert len(meta["positive"]) == MAX_PROMPT_CHARS
    assert len(meta["recipe"]) == MAX_RECIPE_CHARS


def test_read_image_metadata_keys_are_stable(tmp_path):
    path = _write_png(tmp_path / "keys.png", {"parameters": "hi"})
    assert set(read_image_metadata(path)) == {
        "positive",
        "negative",
        "seed",
        "model",
        "sampler",
        "steps",
        "cfg",
        "size",
        "category",
        "recipe",
        "rolls",
    }


def test_read_image_metadata_missing_file_returns_none(tmp_path):
    assert read_image_metadata(tmp_path / "nope.png") is None


# --- endpoint -------------------------------------------------------------


def test_image_metadata_endpoint_returns_parsed_metadata(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _write_png(data_dir / "gen.png", {"parameters": _PARAMETERS_BLOCK, "miso:category": "Landscapes"})

    resp = client.get("/api/image-metadata/gen.png")
    assert resp.status_code == 200
    meta = resp.get_json()["metadata"]
    assert meta["positive"].startswith("a serene lakeside")
    assert meta["seed"] == "326515186"
    assert meta["category"] == "Landscapes"


def test_image_metadata_endpoint_returns_null_for_plain_png(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    resp = client.get("/api/image-metadata/sample.png")
    assert resp.status_code == 200
    assert resp.get_json() == {"metadata": None}


def test_image_metadata_endpoint_returns_null_for_non_png_media(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _write_png(data_dir / "gen.png", {"parameters": _PARAMETERS_BLOCK})
    (data_dir / "gen.png").rename(data_dir / "gen.jpg")

    resp = client.get("/api/image-metadata/gen.jpg")
    assert resp.status_code == 200
    assert resp.get_json() == {"metadata": None}


def test_image_metadata_endpoint_rejects_traversal(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    assert client.get("/api/image-metadata/../../etc/passwd").status_code == 400


def test_image_metadata_endpoint_404s_missing_file(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    resp = client.get("/api/image-metadata/absent.png")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Image not found"}


def test_image_metadata_endpoint_404s_non_media_file(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    (data_dir / "notes.txt").write_text("hello")
    assert client.get("/api/image-metadata/notes.txt").status_code == 404


def test_image_metadata_endpoint_rejects_symlink_outside_data_folder(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    outside = tmp_path / "outside.png"
    _write_png(outside, {"parameters": _PARAMETERS_BLOCK})
    (data_dir / "link.png").symlink_to(outside)

    resp = client.get("/api/image-metadata/link.png")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Image not found"}


def test_image_metadata_endpoint_requires_auth(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path, auth_type="local", extra_env={"ADMIN_PASSWORD": "pass123"})
    resp = client.get("/api/image-metadata/sample.png", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login?next=/api/image-metadata/sample.png" in resp.headers["Location"]


# --- template wiring ------------------------------------------------------


def test_index_and_recent_load_metadata_asset_and_url(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _write_png(data_dir / "gen.png", {"parameters": _PARAMETERS_BLOCK})

    for route in ("/", "/recent"):
        html = client.get(route).get_data(as_text=True)
        assert '<script src="/assets/image-metadata.js" defer></script>' in html, route
        assert "data-metadata-url=\"/api/image-metadata/gen.png\"" in html, route
