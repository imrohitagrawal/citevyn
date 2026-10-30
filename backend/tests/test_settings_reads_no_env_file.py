"""``Settings`` reads the process environment only, never a ``.env`` file.

It used to declare ``env_file=".env"``, resolved against whatever folder the
command ran in: ``make test`` and ``uvicorn`` from ``backend/`` read
``backend/.env``, while ``make migrate``/``seed``/``demo`` ran from the repo root
and read no backend file at all. The Makefile now loads ``backend/.env`` for the
development commands (``scripts/with_env.py``), and the app reads only what it is
given, as it already did on Fly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings


def test_settings_declares_no_env_file() -> None:
    # Red when: `env_file` is set again in Settings.model_config (any value).
    assert Settings.model_config.get("env_file") is None


def test_a_dotenv_in_the_current_folder_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Red when: Settings goes back to `env_file=".env"` (it would read `router` here).
    # The placeholder key keeps a file-reading Settings constructible, so a regression
    # shows up as `router`, not as a validation error about the missing key.
    (tmp_path / ".env").write_text(
        "CITEVYN_LLM_PROVIDER=router\nCITEVYN_OPENROUTER_API_KEY=placeholder-not-a-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CITEVYN_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CITEVYN_OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Settings().llm_provider == "stub"


def test_the_process_environment_is_still_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Partner to the test above: `stub` must not be the only answer Settings can give.
    # Red when: Settings stops reading the environment (a wrong env_prefix, say).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "router")
    monkeypatch.setenv("CITEVYN_OPENROUTER_API_KEY", "placeholder-not-a-key")
    assert Settings().llm_provider == "router"
