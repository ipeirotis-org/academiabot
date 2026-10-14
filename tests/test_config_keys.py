"""config.load_missing_keys(): environment and .env first, then Secret Manager."""
from types import SimpleNamespace

import pytest

from wikidata_discover import config

ENVS = list(config.KEY_SECRETS)


class FakeSecretClient:
    def __init__(self, values=None, fail=()):
        self.values = values or {}
        self.fail = set(fail)
        self.names = []

    def access_secret_version(self, request, **kwargs):
        name = request["name"]
        self.names.append(name)
        secret = name.split("/secrets/")[1].split("/")[0]
        if secret in self.fail:
            raise PermissionError("403")
        return SimpleNamespace(payload=SimpleNamespace(data=self.values[secret].encode()))


@pytest.fixture(autouse=True)
def clean_keys(monkeypatch):
    for env in ENVS:
        monkeypatch.delenv(env, raising=False)
        monkeypatch.setattr(config, env, None)


def test_keys_in_environment_are_not_fetched(monkeypatch):
    for env in ENVS:
        monkeypatch.setenv(env, "local-" + env)

    def no_client():
        raise AssertionError("client must not be built when every key is set")
    monkeypatch.setattr(config, "_new_secret_client", no_client)

    assert config.load_missing_keys() == {env: "env" for env in ENVS}


def test_missing_keys_come_from_secret_manager(monkeypatch):
    import os
    monkeypatch.setenv("OPENAI_API_KEY", "local-openai")
    client = FakeSecretClient({"anthropic-api-key": "sm-anthropic\n", "gemini-api-key": "sm-gemini"})

    sources = config.load_missing_keys(client)

    assert sources == {"OPENAI_API_KEY": "env", "ANTHROPIC_API_KEY": "secret_manager",
                       "GOOGLE_API_KEY": "secret_manager"}
    assert client.names == ["projects/wikidata-academia/secrets/anthropic-api-key/versions/latest",
                            "projects/wikidata-academia/secrets/gemini-api-key/versions/latest"]
    assert os.environ["ANTHROPIC_API_KEY"] == config.ANTHROPIC_API_KEY == "sm-anthropic"
    assert os.environ["GOOGLE_API_KEY"] == config.GOOGLE_API_KEY == "sm-gemini"
    assert os.environ["OPENAI_API_KEY"] == "local-openai"


def test_unreadable_secret_is_skipped(monkeypatch):
    import os
    client = FakeSecretClient({"openai-api-key": "sm-openai", "gemini-api-key": "sm-gemini"},
                              fail={"anthropic-api-key"})

    sources = config.load_missing_keys(client)

    assert sources == {"OPENAI_API_KEY": "secret_manager", "ANTHROPIC_API_KEY": "missing",
                       "GOOGLE_API_KEY": "secret_manager"}
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert config.ANTHROPIC_API_KEY is None


def test_no_credentials_tries_the_client_once(monkeypatch):
    calls = []

    def no_credentials():
        calls.append(1)
        raise RuntimeError("DefaultCredentialsError")
    monkeypatch.setattr(config, "_new_secret_client", no_credentials)

    assert config.load_missing_keys() == {env: "missing" for env in ENVS}
    assert calls == [1]


def test_key_value_is_never_printed(monkeypatch, capsys):
    config.load_missing_keys(FakeSecretClient({s: "secret-value-" + s for s in config.KEY_SECRETS.values()}))
    assert "secret-value" not in capsys.readouterr().out
