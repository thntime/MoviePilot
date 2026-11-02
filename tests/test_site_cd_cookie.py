"""
CD 站点凭据更新应走 CloudSaver 登录测试，而不是网页仿真登录。
"""

from types import SimpleNamespace

from app.chain.site import SiteChain


def test_cd_site_update_cookie_uses_cloudsaver(monkeypatch):
    """
    CD 站点更新凭据时调用 test_netdisk_site，并返回其结果。
    """
    chain = object.__new__(SiteChain)
    calls: list[tuple[str, dict]] = []

    def fake_run_module(name: str, **kwargs):
        """
        记录模块调用并返回成功。
        """
        calls.append((name, kwargs))
        return True, ""

    monkeypatch.setattr(chain, "run_module", fake_run_module)
    site = SimpleNamespace(
        id=7,
        name="cloud saver",
        domain="192.168.3.31",
        url="http://192.168.3.31:8008",
        kind="cd",
        auth_type="auth",
        timeout=30,
        auth_refresh_interval=300,
    )

    state, message = chain.update_cookie(site, "admin", "secret")

    assert state is True
    assert message == "登录成功"
    assert calls[0][0] == "test_netdisk_site"
    assert calls[0][1]["site"]["username"] == "admin"
    assert calls[0][1]["site"]["password"] == "secret"
