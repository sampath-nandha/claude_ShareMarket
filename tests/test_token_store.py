from mmm.broker import auth
from mmm.timeutil import now_ist


class _NoKeyring:
    def get_password(self, *_a):
        raise RuntimeError("no backend")

    def set_password(self, *_a):
        raise RuntimeError("no backend")


def test_session_file_fallback_when_keyring_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    store = auth.TokenStore()
    store._keyring = _NoKeyring()
    assert store.access_token() is None
    store.set_access_token("abc123")
    assert (tmp_path / "token" / "session.json").exists()
    other = auth.TokenStore()
    other._keyring = _NoKeyring()
    assert other.access_token() == "abc123"
    assert other.token_issued_on() == now_ist().date().isoformat()
    settings.get_secrets.cache_clear()
