import pytest

from tapes_map.config import load_settings, parse_duration
from tapes_map.manifest import load_manifest

CONFIG = load_manifest()["config"]


def test_defaults_come_from_the_manifest():
    assert load_settings(CONFIG, {}) == {
        "embeddings_view": "search_contract_v1.span_embeddings",
        "sessions_table": "tapes_v1.sessions",
        "refresh.interval": 3600.0,
        "wait_for_db": False,
    }


def test_keys_map_to_cassette_env_names():
    settings = load_settings(
        CONFIG,
        {
            "CASSETTE_EMBEDDINGS_VIEW": "contract.embeddings",
            "CASSETTE_SESSIONS_TABLE": "tapes_v1.sessions",
            "CASSETTE_REFRESH_INTERVAL": "1h30m",
            "CASSETTE_WAIT_FOR_DB": "true",
        },
    )
    assert settings["embeddings_view"] == "contract.embeddings"
    assert settings["refresh.interval"] == 5400.0
    assert settings["wait_for_db"] is True


@pytest.mark.parametrize(
    "env",
    [{"CASSETTE_REFRESH_INTERVAL": "60"}, {"CASSETTE_REFRESH_INTERVAL": "0s"}, {"CASSETTE_WAIT_FOR_DB": "yes"}],
)
def test_invalid_values_are_rejected(env):
    with pytest.raises(ValueError, match="CASSETTE_"):
        load_settings(CONFIG, env)


@pytest.mark.parametrize("raw,seconds", [("90s", 90), ("1.5h", 5400), ("500ms", 0.5), ("2m30s", 150)])
def test_go_durations(raw, seconds):
    assert parse_duration(raw) == seconds
