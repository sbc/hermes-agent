"""Slack status payload contracts and Agent Sessions title routing (#110374, #132594)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod


@pytest.fixture(autouse=True)
def _pin_legacy_assistant_threads_api():
    """Pin the SDK capability probe to the legacy assistant.threads API and
    restore it afterwards, so tests that flip the cached flag on don't leak."""
    prev = _slack_mod._AGENT_SESSIONS_SUPPORTED
    _slack_mod._AGENT_SESSIONS_SUPPORTED = False
    yield
    _slack_mod._AGENT_SESSIONS_SUPPORTED = prev


class TestAgentSessionsApiRouting:
    """slack-sdk 3.44.0 Agent Sessions API (assistant_view deprecation Feb 2027).

    Status text uses the assistant compatibility API. Agent Sessions accepts only
    lifecycle enums for status, while its title method still accepts free text.
    """

    def _adapter(self):
        config = PlatformConfig(enabled=True, token="xoxb-fake-token")
        a = SlackAdapter(config)
        a._app = MagicMock()
        a._app.client = AsyncMock()
        return a

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_sessions_supported", [False, True])
    @pytest.mark.parametrize("status_text", [None, "is reviewing your input…", "is reading notes.md…"])
    async def test_status_text_and_clear_obey_slack_api_contract(self, agent_sessions_supported, status_text):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = agent_sessions_supported
        a = self._adapter()
        async def lifecycle_status(**payload):
            if payload["status"] not in {"active", "processing", "suspended", "closed"}:
                raise ValueError("invalid_arguments: status must be a valid enum value")

        a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=lifecycle_status)
        a._app.client.assistant_threads_setStatus = AsyncMock()
        a.config.typing_status_text = status_text
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status=status_text or "is thinking...",
        )
        a._app.client.assistant_threads_setStatus.reset_mock()
        await a.stop_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="",
        )
        a._app.client.agents_sessions_setStatus.assert_not_awaited()
        assert not a._active_status_threads

    @pytest.mark.asyncio
    async def test_thread_title_uses_agents_sessions_rename(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        a._app.client.agents_sessions_rename = AsyncMock()
        a._app.client.assistant_threads_setTitle = AsyncMock()
        await a._set_assistant_thread_title("D123", "171234.0001", "Summarize the incident")
        a._app.client.agents_sessions_rename.assert_called_once_with(
            channel_id="D123",
            thread_ts="171234.0001",
            title="Summarize the incident",
        )
        a._app.client.assistant_threads_setTitle.assert_not_called()
