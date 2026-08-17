import pytest



def test_serve_refuses_unauthenticated_non_loopback_bind(monkeypatch):
    """An unauthenticated API must never be reachable from another host.

    With authentication disabled every protected route receives a synthetic
    root principal, so publishing a non-loopback port hands full administrative
    access to any client that can reach it. Startup must fail closed instead.
    """
    import typer

    from mnemos.cli import main as cli

    monkeypatch.delenv(cli.UNSAFE_NETWORK_BIND_ENV, raising=False)

    class _Auth:
        enabled = False

    class _Settings:
        auth = _Auth()

    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())

    # Loopback binds are always fine, authenticated or not.
    for host in ("127.0.0.1", "::1", "localhost"):
        cli._refuse_unauthenticated_network_bind(host, "test")

    # Anything reachable off-host must be refused while auth is disabled.
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.internal"):
        with pytest.raises(typer.BadParameter) as excinfo:
            cli._refuse_unauthenticated_network_bind(host, "test")
        assert "authentication disabled" in str(excinfo.value)

    # The documented escape hatch must still work, and only via that variable.
    monkeypatch.setenv(cli.UNSAFE_NETWORK_BIND_ENV, "1")
    cli._refuse_unauthenticated_network_bind("0.0.0.0", "test")


def test_serve_allows_non_loopback_bind_once_auth_is_enabled(monkeypatch):
    from mnemos.cli import main as cli

    monkeypatch.delenv(cli.UNSAFE_NETWORK_BIND_ENV, raising=False)

    class _Auth:
        enabled = True

    class _Settings:
        auth = _Auth()

    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    cli._refuse_unauthenticated_network_bind("0.0.0.0", "test")
