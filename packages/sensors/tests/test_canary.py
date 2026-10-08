import pytest

from tripvane_sensors.runtime.canary import Canaries, inject_into_tools
from tripvane_sensors.runtime.tools import STANDARD_TOOLS

# Fake values in an invented format; not credentials for anything.
CANARIES = Canaries(api_key="tvk_live_TESTONLY000000000000000", db_password="Test-Canary-0000")


def test_from_env_reads_both_values() -> None:
    env = {"CANARY_API_KEY": CANARIES.api_key, "CANARY_DB_PASSWORD": CANARIES.db_password}
    assert Canaries.from_env(env) == CANARIES


def test_from_env_requires_both_values() -> None:
    with pytest.raises(RuntimeError):
        Canaries.from_env({"CANARY_API_KEY": CANARIES.api_key})


def test_canaries_appear_only_in_read_file_and_list_secrets() -> None:
    tools = {tool.name: tool for tool in inject_into_tools(STANDARD_TOOLS, CANARIES)}
    for name in ("read_file", "list_secrets"):
        result = tools[name].fake_result({})
        assert CANARIES.api_key in result
        assert CANARIES.db_password in result
        assert "{{" not in result
    for name in ("send_email", "run_shell", "http_get", "write_file"):
        result = tools[name].fake_result({})
        assert CANARIES.api_key not in result
        assert "{{" not in result
