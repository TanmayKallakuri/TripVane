import shutil
from pathlib import Path

import pytest

from tripvane_sensors.runtime.replay import main

FIXTURES = Path(__file__).parents[3] / "fixtures"


def test_starter_fixtures_replay_cleanly() -> None:
    assert main([str(FIXTURES)]) == 0


@pytest.fixture
def fixtures_copy(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, target)
    return target


def test_a_changed_expected_event_fails(fixtures_copy: Path) -> None:
    expected = fixtures_copy / "benign_question.expected.jsonl"
    expected.write_text(expected.read_text().replace('"completed"', '"error"'))
    assert main([str(fixtures_copy)]) == 1


def test_an_unused_scripted_turn_fails(fixtures_copy: Path) -> None:
    script = fixtures_copy / "benign_question.script.json"
    shutil.copy(fixtures_copy / "indirect_injection_shell.script.json", script)
    assert main([str(fixtures_copy)]) == 1
