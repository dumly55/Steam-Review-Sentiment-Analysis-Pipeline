from steam_analyzer.config import load_settings


def test_defaults_to_helldivers_2(monkeypatch):
    monkeypatch.delenv("STEAM_APP_ID", raising=False)
    assert load_settings().steam_app_id == 553850


def test_app_id_is_configurable(monkeypatch):
    monkeypatch.setenv("STEAM_APP_ID", "620")
    assert load_settings().steam_app_id == 620
