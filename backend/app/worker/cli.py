"""Console-script entry point for the ingestion worker.

Slice 8 stub. The full polling loop, fetcher registry, parser, chunker,
exact-term extractor, embedder, and error types land in step 6 of the
Slice 8 build. This stub is here so the ``citevyn-worker`` console
script defined in :file:`pyproject.toml` is importable during the
earlier steps of the slice.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Print a one-line notice and exit non-zero.

    The real entry point is wired in step 6; until then invoking the
    console script surfaces a clear "not implemented" message instead
    of an ImportError trace.
    """
    print(
        "citevyn-worker: Slice 8 stub. The polling loop is implemented in step 6.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
