"""Every local asset referenced by the frontend shell must actually ship (#221).

``frontend/index.html`` carried ``<link rel="icon" href="/favicon.svg">`` while
``frontend/public/`` did not exist. That dead reference survived a Vite build, a Docker
image, CI and a production deploy without anything noticing, and every browser tab for
the public demo showed a blank icon as a result.

A favicon that 404s is cosmetic. The *class* is not: a hero image, an OG preview or a
web-app manifest referenced the same way would fail exactly as silently. So this module
does not assert "a favicon exists" — it parses the shell, extracts EVERY local asset
reference, and asserts each one resolves to a real file. Adding a new ``<link>`` or
``<img>`` to a file that was never created now fails the build.

Resolution mirrors how Vite actually serves these:

* ``/src/...`` is a *source* entry Vite rewrites into a hashed bundle at build time, so
  it resolves against ``frontend/``;
* everything else is a ``publicDir`` asset copied verbatim into ``dist/``, so it
  resolves against ``frontend/public/``.

Remote ``http(s)://`` and ``data:`` references are out of scope — this guards what the
repo is responsible for shipping, not third-party font CDNs.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import struct
import zlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
INDEX_HTML = FRONTEND / "index.html"
PUBLIC = FRONTEND / "public"
DIST = FRONTEND / "dist"

# Deliberately attribute-based rather than tag-based, so a new element type (``<img>``,
# ``<use>``) is covered for free. ``content`` is included because that is how social
# preview images are declared — ``<meta property="og:image" content="/og.png">`` — and an
# OG image is precisely the "worse than a favicon" case this module exists to catch.
_REF_RE = re.compile(r"""\b(href|src|content)\s*=\s*["']([^"']+)["']""")

_REMOTE_PREFIXES = ("http://", "https://", "data:", "//", "mailto:", "#")


def _local_refs(html: str) -> list[str]:
    """Local asset references in ``html`` — remote and inline URLs dropped.

    ``content`` carries free prose as often as it carries a path (``<meta
    name="description">``, ``theme-color``), so a ``content`` value counts only when it
    is shaped like one. ``href``/``src`` are always references, so they are not narrowed
    that way — a bare relative ``href="app.css"`` must still be checked.
    """
    refs = []
    for attr, value in _REF_RE.findall(html):
        if value.startswith(_REMOTE_PREFIXES):
            continue
        if attr == "content" and not value.startswith(("/", "./", "../")):
            continue
        refs.append(value)
    return refs


def _resolve(ref: str) -> pathlib.Path:
    """Where ``ref`` must exist in the SOURCE tree for the build to emit it."""
    rel = ref.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    # Vite rewrites the /src entry into a hashed asset; everything else is publicDir.
    return (FRONTEND / rel) if rel.startswith("src/") else (PUBLIC / rel)


@pytest.fixture(scope="module")
def refs() -> list[str]:
    return _local_refs(INDEX_HTML.read_text(encoding="utf-8"))


def test_the_parser_actually_found_local_references(refs: list[str]) -> None:
    """Vacuous-pass guard: a regex that matches nothing would make every test below pass.

    The shell ships the module entry plus the icon set, so the real count is well above
    this floor; the assertion only has to catch a parser that silently stopped working.
    """
    assert len(refs) >= 4, f"expected the shell to reference several local assets, got {refs}"


def test_every_local_asset_referenced_by_index_html_exists(refs: list[str]) -> None:
    """THE #221 regression: index.html referenced /favicon.svg, which was never created."""
    missing = [ref for ref in refs if not _resolve(ref).is_file()]
    assert not missing, (
        "frontend/index.html references local assets that do not exist: "
        + ", ".join(f"{ref} (looked for {_resolve(ref)})" for ref in missing)
    )


def test_the_favicon_reference_that_regressed_is_covered(refs: list[str]) -> None:
    """Pin the specific instance, so a refactor of the parser cannot quietly drop it."""
    assert "/favicon.svg" in refs
    assert (PUBLIC / "favicon.svg").is_file()


def test_every_icon_declared_by_the_web_manifest_exists() -> None:
    """The manifest is a second document that can name a file nobody shipped."""
    manifest = json.loads((PUBLIC / "site.webmanifest").read_text(encoding="utf-8"))
    missing = [icon["src"] for icon in manifest["icons"] if not _resolve(icon["src"]).is_file()]
    assert not missing, f"site.webmanifest references missing icons: {missing}"


def test_conventional_root_requests_are_served() -> None:
    """Clients request /favicon.ico by convention no matter what the <link> tags say."""
    for name in ("favicon.ico", "apple-touch-icon.png"):
        assert (PUBLIC / name).is_file(), f"frontend/public/{name} is missing"


def _decode_png(blob: bytes) -> tuple[int, int, bytes]:
    """Return ``(width, height, raw RGBA)`` for a PNG written by the generator.

    Only the shape the generator emits is supported: 8-bit RGBA, no interlacing, and
    filter type 0 on every row. Anything else means the file did not come from
    ``gen_favicon.py``, so raising is the correct outcome rather than a silent skip.
    """
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, width, height, idat = 8, 0, 0, bytearray()
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos : pos + 4])
        tag = blob[pos + 4 : pos + 8]
        data = blob[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", data[:10])
            assert (depth, colour) == (8, 6), f"expected 8-bit RGBA, got {depth=} {colour=}"
            assert data[12] == 0, "interlaced PNGs are not supported"
        elif tag == b"IDAT":
            idat += data
        pos += 12 + length

    raw, stride = zlib.decompress(bytes(idat)), width * 4
    out = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        assert raw[start] == 0, f"row {y} uses filter {raw[start]}, expected 0"
        out += raw[start + 1 : start + 1 + stride]
    return width, height, bytes(out)


def _png_entries(name: str, blob: bytes) -> list[bytes]:
    """The PNG payload(s) inside ``blob`` — unwrapping the ICO container if needed."""
    if not name.endswith(".ico"):
        return [blob]
    _, _, count = struct.unpack("<HHH", blob[:6])
    entries = []
    for i in range(count):
        size, offset = struct.unpack("<II", blob[6 + 16 * i + 8 : 6 + 16 * i + 16])
        entries.append(blob[offset : offset + size])
    return entries


def test_the_committed_raster_icons_match_their_generator() -> None:
    """The binaries are generated, so they must still depict what the generator draws.

    Without this, a hand-edited or stale ``favicon.ico`` would sit in the tree looking
    authoritative while ``scripts/gen_favicon.py`` — the thing that documents the
    geometry — silently disagreed with it.

    Compares DECODED PIXELS, not file bytes. ``zlib`` deflate output is stable for a
    given zlib build but is NOT guaranteed identical across zlib versions, and CI does
    not run the same build as any given developer machine. Comparing compressed bytes
    would therefore have been a gate that fails on an innocent toolchain difference —
    a flaky test, which ``code_review.md`` blocks shipping on. Pixels are the property
    actually worth asserting, and they are toolchain-independent.
    """
    spec = importlib.util.spec_from_file_location(
        "gen_favicon", REPO_ROOT / "scripts" / "gen_favicon.py"
    )
    assert spec is not None and spec.loader is not None
    gen_favicon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_favicon)

    drifted: list[str] = []
    for name, expected_blob in gen_favicon.build().items():
        committed = (PUBLIC / name).read_bytes()
        for got, want in zip(
            _png_entries(name, committed),
            _png_entries(name, expected_blob),
            strict=True,
        ):
            gw, gh, gpx = _decode_png(got)
            ww, wh, wpx = _decode_png(want)
            if (gw, gh, gpx) != (ww, wh, wpx):
                drifted.append(f"{name}@{ww}x{wh}")
    assert not drifted, (
        "committed icons no longer depict scripts/gen_favicon.py output "
        f"({drifted}); re-run `uv run python scripts/gen_favicon.py`"
    )


@pytest.mark.skipif(not DIST.is_dir(), reason="frontend/dist is gitignored; build first")
def test_every_local_asset_resolves_in_the_build_output(refs: list[str]) -> None:
    """When a build exists, assert the STRONGER claim: the asset is in what ships.

    ``dist/`` is gitignored, so this cannot be the primary gate — it skips on a clean
    checkout. It runs for anyone who has built, and inside the API image build, where it
    is the closest available proxy for "what production actually serves".
    """
    built = (DIST / "index.html").read_text(encoding="utf-8")
    missing = [
        ref for ref in _local_refs(built) if not (DIST / ref.split("?", 1)[0].lstrip("/")).is_file()
    ]
    assert not missing, f"built dist/index.html references assets absent from dist/: {missing}"
