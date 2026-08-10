from mnemos.installer import agent
from mnemos.installer.wizard import Config


def test_run_agent_uses_shared_installer_config(monkeypatch):
    expected = Config(profile="edge")
    monkeypatch.setattr(agent.AgentInstaller, "run", lambda self: expected)

    result = agent.run_agent(agent.SystemInfo())

    assert result is expected
    assert isinstance(result, Config)


def test_agent_profiles_are_canonicalized():
    assert agent._canonical_profile("personal") == "edge"
    assert agent._canonical_profile("team") == "server"
    assert agent._canonical_profile("enterprise") == "server"
