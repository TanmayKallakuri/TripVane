from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tripvane_analyst.cli import main


def test_campaigns_runs_without_a_model_key(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    add_payload(payload_texts["campaign_a1"], asn=64500, is_attack=True)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'analyst.db'}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["campaigns"]) == 0
    assert capsys.readouterr().out == "campaigns: assigned=1 campaigns=1 cleared=0\n"
    assert main(["campaigns"]) == 0
    assert capsys.readouterr().out == "campaigns: nothing pending\n"


def test_model_steps_need_the_api_key(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'analyst.db'}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for command in ("gate", "tag", "novel", "batch-submit", "batch-collect"):
        assert main([command]) == 2


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["campaigns"]) == 2


def test_unknown_commands_are_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["publish"])
