"""Slack inbound documents and forwarded-message regressions (#96384)."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType
from plugins.platforms.slack.adapter import SlackAdapter
from tests.gateway import test_slack as slack_tests

adapter = slack_tests.adapter
_redirect_cache = slack_tests._redirect_cache
_rich_text_blocks = slack_tests._rich_text_blocks
_rich_text_section = slack_tests._rich_text_section

class TestIncomingDocumentHandling:
    def _make_event(
        self, files=None, text="hello", channel_type="im", blocks=None, attachments=None
    ):
        """Build a mock Slack message event with file attachments."""
        return {
            "text": text,
            "user": "U_USER",
            "channel": "D123",
            "channel_type": channel_type,
            "ts": "1234567890.000001",
            "files": files or [],
            "blocks": blocks or [],
            "attachments": attachments or [],
        }

    @pytest.mark.asyncio
    async def test_pdf_document_cached(self, adapter):
        """A PDF attachment should be downloaded, cached, and set as DOCUMENT type."""
        pdf_bytes = b"%PDF-1.4 fake content"

        with patch.object(
            adapter, "_download_slack_file_bytes", new_callable=AsyncMock
        ) as dl:
            dl.return_value = pdf_bytes
            event = self._make_event(
                files=[
                    {
                        "mimetype": "application/pdf",
                        "name": "report.pdf",
                        "url_private_download": "https://files.slack.com/report.pdf",
                        "size": len(pdf_bytes),
                    }
                ]
            )
            await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.message_type == MessageType.DOCUMENT
        assert len(msg_event.media_urls) == 1
        assert os.path.exists(msg_event.media_urls[0])
        assert msg_event.media_types == ["application/pdf"]


    @pytest.mark.asyncio
    async def test_txt_document_injects_content(self, adapter):
        """A .txt file under 100KB should have its content injected into event text."""
        content = b"Hello from a text file"

        with patch.object(
            adapter, "_download_slack_file_bytes", new_callable=AsyncMock
        ) as dl:
            dl.return_value = content
            event = self._make_event(
                text="summarize this",
                files=[
                    {
                        "mimetype": "text/plain",
                        "name": "notes.txt",
                        "url_private_download": "https://files.slack.com/notes.txt",
                        "size": len(content),
                    }
                ],
            )
            await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert "Hello from a text file" in msg_event.text
        assert "[Content of notes.txt]" in msg_event.text
        assert "summarize this" in msg_event.text


    @pytest.mark.asyncio
    async def test_json_snippet_injects_content(self, adapter):
        """A .json snippet should be treated as a text document and injected."""
        content = b'{"hello": "world", "count": 2}'

        with patch.object(
            adapter, "_download_slack_file_bytes", new_callable=AsyncMock
        ) as dl:
            dl.return_value = content
            event = self._make_event(
                text="can you parse this",
                files=[
                    {
                        "mimetype": "text/plain",
                        "name": "zapfile.json",
                        "filetype": "json",
                        "pretty_type": "JSON",
                        "mode": "snippet",
                        "editable": True,
                        "url_private_download": "https://files.slack.com/zapfile.json",
                        "size": len(content),
                    }
                ],
            )
            await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.message_type == MessageType.DOCUMENT
        assert len(msg_event.media_urls) == 1
        assert msg_event.media_types == ["application/json"]
        assert "[Content of zapfile.json]" in msg_event.text
        assert '"hello": "world"' in msg_event.text
        assert "can you parse this" in msg_event.text

    @pytest.mark.asyncio
    async def test_forwarded_message_file_is_downloaded(self, adapter):
        """A file attached to a forwarded/shared message (Slack puts it in
        event.attachments[].files, flagged is_share — NOT in event.files)
        must be downloaded and processed exactly like a direct attachment
        (#96384, related to #75481)."""
        pdf_bytes = b"%PDF-1.4 forwarded content"

        with patch.object(
            adapter, "_download_slack_file_bytes", new_callable=AsyncMock
        ) as dl:
            dl.return_value = pdf_bytes
            event = self._make_event(
                text="fyi",
                files=[],
                attachments=[
                    {
                        "is_share": True,
                        "author_name": "Alice",
                        "text": "check this out",
                        "files": [
                            {
                                "mimetype": "application/pdf",
                                "name": "forwarded-report.pdf",
                                "url_private_download": "https://files.slack.com/forwarded-report.pdf",
                                "size": len(pdf_bytes),
                            }
                        ],
                    }
                ],
            )
            await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.message_type == MessageType.DOCUMENT
        assert len(msg_event.media_urls) == 1
        assert os.path.exists(msg_event.media_urls[0])
        assert msg_event.media_types == ["application/pdf"]

    @pytest.mark.asyncio
    async def test_forwarded_message_stub_file_hydrated_via_files_info(self, adapter):
        """A forwarded message's file reference can arrive as a Slack Connect
        stub (file_access=check_file_info, no URL fields). The adapter must
        call files.info to hydrate the full object before downloading it."""
        pdf_bytes = b"%PDF-1.4 hydrated forwarded content"
        adapter._app.client.files_info = AsyncMock(
            return_value={
                "ok": True,
                "file": {
                    "id": "F_STUB",
                    "mimetype": "application/pdf",
                    "name": "hydrated.pdf",
                    "url_private_download": "https://files.slack.com/hydrated.pdf",
                    "size": len(pdf_bytes),
                },
            }
        )

        with patch.object(
            adapter, "_download_slack_file_bytes", new_callable=AsyncMock
        ) as dl:
            dl.return_value = pdf_bytes
            event = self._make_event(
                text="from another workspace",
                files=[],
                attachments=[
                    {
                        "is_share": True,
                        "files": [
                            {
                                "id": "F_STUB",
                                "file_access": "check_file_info",
                            }
                        ],
                    }
                ],
            )
            await adapter._handle_slack_message(event)

        adapter._app.client.files_info.assert_awaited_once_with(file="F_STUB")
        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.message_type == MessageType.DOCUMENT
        assert len(msg_event.media_urls) == 1
        assert msg_event.media_types == ["application/pdf"]

    @pytest.mark.asyncio
    async def test_non_share_attachment_files_are_ignored(self, adapter):
        """Link-unfurl attachments must not have their nested files pulled in;
        only genuine forward/share/reply-unfurl attachments contribute files."""
        event = self._make_event(
            text="check this link",
            files=[],
            attachments=[
                {
                    "title": "Some Notion page",
                    "title_link": "https://notion.so/page",
                    "files": [
                        {
                            "mimetype": "application/pdf",
                            "name": "unrelated.pdf",
                            "url_private_download": "https://files.slack.com/unrelated.pdf",
                        }
                    ],
                }
            ],
        )
        await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.media_urls == []


    @pytest.mark.asyncio
    @pytest.mark.parametrize("content, inlined", [(b"small text", True), (b"x" * (200 * 1024), False)], ids=["small", "large"])
    async def test_document_marks_media_text_inlined(self, adapter, content, inlined):
        """The per-attachment flag must track whether the text was injected, so the document
        note never claims the content is inlined when the >100 KB gate skipped it."""
        with patch.object(
            adapter, "_download_slack_file_bytes", new_callable=AsyncMock
        ) as dl:
            dl.return_value = content
            event = self._make_event(
                files=[
                    {
                        "mimetype": "text/plain",
                        "name": "notes.txt",
                        "url_private_download": "https://files.slack.com/notes.txt",
                        "size": len(content),
                    }
                ],
                text="",
            )
            await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert ("[Content of" in (msg_event.text or "")) is inlined
        assert msg_event.media_text_inlined == [inlined]


    @pytest.mark.asyncio
    async def test_unauthorized_message_does_not_fetch_file_info(
        self,
        adapter,
        monkeypatch,
    ):
        """Global gateway auth must run before Slack file metadata fetches."""
        monkeypatch.delenv("SLACK_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
        monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "U_ALLOWED")

        class Runner:
            def _is_user_authorized(self, source):
                return source.user_id == "U_ALLOWED"

            async def handle(self, _event):
                raise AssertionError("gateway handler should not run")

        adapter._message_handler = Runner().handle
        adapter._app.client.files_info = AsyncMock()

        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "D123",
                "channel_type": "im",
                "user": "U_INTRUDER",
                "text": "please read this",
                "ts": "1234567890.000001",
                "files": [
                    {
                        "id": "FSECRET",
                        "mimetype": "text/plain",
                        "name": "secret.txt",
                    }
                ],
            }
        )

        adapter._app.client.files_info.assert_not_awaited()
        adapter.handle_message.assert_not_called()


    @pytest.mark.asyncio
    async def test_rich_text_quotes_and_lists_are_extracted(self, adapter):
        """Nested quote and list content should be surfaced from rich_text blocks."""
        event = self._make_event(
            text="Can you summarize this?",
            blocks=[
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_quote",
                            "elements": [
                                {
                                    "type": "rich_text_section",
                                    "elements": [
                                        {"type": "text", "text": "Quoted line"}
                                    ],
                                }
                            ],
                        },
                        {
                            "type": "rich_text_list",
                            "style": "bullet",
                            "elements": [
                                {
                                    "type": "rich_text_section",
                                    "elements": [
                                        {"type": "text", "text": "First bullet"}
                                    ],
                                },
                                {
                                    "type": "rich_text_section",
                                    "elements": [
                                        {"type": "text", "text": "Second bullet"}
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        )

        await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert "Can you summarize this?" in msg_event.text
        assert "> Quoted line" in msg_event.text
        assert "• First bullet" in msg_event.text
        assert "• Second bullet" in msg_event.text

    @pytest.mark.parametrize(
        ("text", "section_elements"),
        [
            (
                "update the path to `src/app`",
                [
                    {"type": "text", "text": "update the path to "},
                    {"type": "text", "text": "src/app", "style": {"code": True}},
                ],
            ),
            (
                "use *bold* and _italic_ text",
                [
                    {"type": "text", "text": "use "},
                    {"type": "text", "text": "bold", "style": {"bold": True}},
                    {"type": "text", "text": " and "},
                    {"type": "text", "text": "italic", "style": {"italic": True}},
                    {"type": "text", "text": " text"},
                ],
            ),
            (
                "use *_~styled~_* text",
                [
                    {"type": "text", "text": "use "},
                    {
                        "type": "text",
                        "text": "styled",
                        "style": {"bold": True, "italic": True, "strike": True},
                    },
                    {"type": "text", "text": " text"},
                ],
            ),
            (
                "read <https://example.com/docs|the docs>",
                [
                    {"type": "text", "text": "read "},
                    {
                        "type": "link",
                        "url": "https://example.com/docs",
                        "text": "the docs",
                    },
                ],
            ),
        ],
        ids=("inline-code", "inline-styles", "nested-inline-styles", "link"),
    )
    @pytest.mark.asyncio
    async def test_equivalent_rich_text_is_not_duplicated(
        self, adapter, text, section_elements
    ):
        event = self._make_event(
            text=text,
            blocks=_rich_text_blocks(_rich_text_section(*section_elements)),
        )

        await adapter._handle_slack_message(event)

        assert adapter.handle_message.call_args[0][0].text == text

    @pytest.mark.parametrize(
        "text",
        (
            "run ```echo ok```",
            "run\n\n```\necho ok\n```\n",
        ),
        ids=("compact-fence", "fence-with-surrounding-newlines"),
    )
    @pytest.mark.asyncio
    async def test_equivalent_preformatted_text_is_not_duplicated(
        self, adapter, text
    ):
        event = self._make_event(
            text=text,
            blocks=_rich_text_blocks(
                _rich_text_section({"type": "text", "text": "run"}),
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": "echo ok"}],
                },
            ),
        )

        await adapter._handle_slack_message(event)

        assert adapter.handle_message.call_args[0][0].text == text

    @pytest.mark.asyncio
    async def test_preformatted_text_also_mentioned_in_prose_is_preserved(self, adapter):
        event = self._make_event(
            text="run echo ok to verify the command",
            blocks=_rich_text_blocks(
                _rich_text_section(
                    {"type": "text", "text": "run echo ok to verify the command"}
                ),
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": "echo ok"}],
                },
            ),
        )

        await adapter._handle_slack_message(event)

        assert adapter.handle_message.call_args[0][0].text == (
            "run echo ok to verify the command\n```\necho ok\n```"
        )

    @pytest.mark.asyncio
    async def test_block_only_bot_mention_does_not_duplicate_rich_text(self, adapter):
        event = self._make_event(
            text="update the path",
            blocks=_rich_text_blocks(
                _rich_text_section(
                    {"type": "user", "user_id": "U_BOT"},
                    {"type": "text", "text": " update the path"},
                )
            ),
        )

        await adapter._handle_slack_message(event)

        assert adapter.handle_message.call_args[0][0].text == "update the path"

    @pytest.mark.asyncio
    async def test_secondary_workspace_bot_mention_does_not_duplicate_rich_text(
        self, adapter
    ):
        adapter._team_bot_user_ids["T_SECONDARY"] = "U_SECONDARY_BOT"
        event = self._make_event(
            text="update the path",
            blocks=_rich_text_blocks(
                _rich_text_section(
                    {"type": "user", "user_id": "U_SECONDARY_BOT"},
                    {"type": "text", "text": " update the path"},
                )
            ),
        )

        await adapter._handle_slack_message(event, {"team_id": "T_SECONDARY"})

        assert adapter.handle_message.call_args[0][0].text == "update the path"

    @pytest.mark.asyncio
    async def test_rich_text_list_already_in_text_is_not_duplicated(self, adapter):
        event = self._make_event(
            text="• first\n• second",
            blocks=_rich_text_blocks(
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "elements": [
                        _rich_text_section({"type": "text", "text": "first"}),
                        _rich_text_section({"type": "text", "text": "second"}),
                    ],
                }
            ),
        )

        await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.text == "• first\n• second"

    @pytest.mark.asyncio
    async def test_rich_text_different_section_is_preserved(self, adapter):
        event = self._make_event(
            text="review `test/yana`",
            blocks=_rich_text_blocks(
                _rich_text_section(
                    {"type": "text", "text": "also review test/prod"}
                )
            ),
        )

        await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.text == "review `test/yana`\nalso review test/prod"

    @pytest.mark.asyncio
    async def test_rich_text_duplicate_section_keeps_quote(self, adapter):
        event = self._make_event(
            text="review `test/yana`",
            blocks=_rich_text_blocks(
                _rich_text_section(
                    {"type": "text", "text": "review "},
                    {"type": "text", "text": "test/yana", "style": {"code": True}},
                ),
                {
                    "type": "rich_text_quote",
                    "elements": [
                        _rich_text_section(
                            {"type": "text", "text": "quoted context"}
                        )
                    ],
                },
            ),
        )

        await adapter._handle_slack_message(event)

        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.text == "review `test/yana`\n> quoted context"


class TestForwardedSlackMessages:
    """Shared Slack messages keep their provenance, text, and files."""

    @staticmethod
    def _event(attachment, *, text="review this", team="T_TEAM", channel="D123"):
        return {
            "text": text,
            "user": "U_USER",
            "channel": channel,
            "channel_type": "im",
            "client_msg_id": f"client-{channel}",
            "ts": f"1234567890.{len(channel):06d}",
            "team": team,
            "files": [],
            "attachments": [attachment],
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ("is_share", "is_msg_unfurl", "is_reply_unfurl"))
    async def test_all_forwarding_flags_preserve_text(self, adapter, flag):
        await adapter._handle_slack_message(
            self._event(
                {
                    flag: True,
                    "author_id": "U_OTHER",
                    "author_name": "Alice",
                    "title": "Original incident report",
                    "text": "The staging deploy is waiting for approval.",
                    "fallback": "The staging deploy is waiting for approval.",
                    "from_url": "https://workspace.slack.com/archives/C123/p123",
                }
            )
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert "[Forwarded Slack message" in rendered
        assert "Original incident report" in rendered
        assert "The staging deploy is waiting for approval." in rendered
        assert rendered.count("The staging deploy is waiting for approval.") == 1
        assert "Link: https://workspace.slack.com/archives/C123/p123" in rendered
        assert "[End forwarded Slack message]" in rendered

    @pytest.mark.asyncio
    async def test_forwarded_text_can_come_only_from_message_blocks(self, adapter):
        await adapter._handle_slack_message(
            self._event(
                {
                    "is_share": True,
                    "author_id": "U_OTHER",
                    "message_blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "Only the Block Kit payload has this update.",
                            },
                        }
                    ],
                }
            )
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert "Only the Block Kit payload has this update." in rendered

    @pytest.mark.asyncio
    async def test_nested_message_fields_and_fallback_are_deduplicated(self, adapter):
        await adapter._handle_slack_message(
            self._event(
                {
                    "is_share": True,
                    "message": {
                        "author_id": "U_OTHER",
                        "title": "Nested report",
                        "text": "The database is healthy.",
                        "fallback": "The database is healthy.",
                    },
                }
            )
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert rendered.count("The database is healthy.") == 1
        assert "Nested report" in rendered

    @pytest.mark.asyncio
    async def test_forwarded_message_longer_than_link_preview_limit_is_kept(self, adapter):
        long_text = "Forwarded details: " + ("x" * 650)
        await adapter._handle_slack_message(
            self._event({"is_share": True, "author_id": "U_OTHER", "text": long_text})
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert long_text in rendered
        assert len(rendered) > 500

    @pytest.mark.asyncio
    async def test_missing_author_fails_open(self, adapter):
        await adapter._handle_slack_message(
            self._event({"is_msg_unfurl": True, "text": "Unattributed forwarded details"})
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert "Unattributed forwarded details" in rendered

    @pytest.mark.asyncio
    async def test_explicit_share_of_hermes_message_is_kept(self, adapter):
        await adapter._handle_slack_message(
            self._event(
                {
                    "is_share": True,
                    "is_msg_unfurl": True,
                    "author_id": "U_BOT",
                    "text": "A user intentionally forwarded this Hermes answer.",
                }
            )
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert "A user intentionally forwarded this Hermes answer." in rendered

    @pytest.mark.asyncio
    async def test_automatic_preview_of_hermes_message_is_skipped(self, adapter):
        await adapter._handle_slack_message(
            self._event(
                {
                    "is_msg_unfurl": True,
                    "author_id": "U_BOT",
                    "text": "An earlier Hermes answer should not repeat.",
                }
            )
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert "An earlier Hermes answer should not repeat." not in rendered

    @pytest.mark.asyncio
    async def test_self_preview_uses_the_bot_id_for_that_workspace(self, adapter):
        adapter._team_bot_user_ids = {"T_ONE": "U_ONE", "T_TWO": "U_TWO"}
        await adapter._handle_slack_message(
            self._event(
                {
                    "is_msg_unfurl": True,
                    "author_id": "U_TWO",
                    "text": "A message from the other workspace bot.",
                },
                team="T_ONE",
                channel="D_ONE",
            )
        )
        await adapter._handle_slack_message(
            self._event(
                {
                    "is_msg_unfurl": True,
                    "author_id": "U_TWO",
                    "text": "This is the current workspace bot preview.",
                },
                team="T_TWO",
                channel="D_TWO",
            )
        )

        first = adapter.handle_message.call_args_list[0].args[0].text
        second = adapter.handle_message.call_args_list[1].args[0].text
        assert "A message from the other workspace bot." in first
        assert "This is the current workspace bot preview." not in second

    def test_live_and_history_render_the_same_forwarded_text(self, adapter):
        attachment = {
            "is_reply_unfurl": True,
            "author_id": "U_OTHER",
            "title": "Shared runbook",
            "text": "Use the staged rollout procedure.",
        }

        live = adapter._append_link_unfurls("review this", [attachment], bot_uid="U_BOT")
        history = adapter._render_message_text(
            {"text": "review this", "attachments": [attachment]}, bot_uid="U_BOT"
        )

        for value in ("Shared runbook", "Use the staged rollout procedure."):
            assert value in live
            assert value in history

    @pytest.mark.asyncio
    async def test_forwarded_files_are_deduplicated_and_full_record_wins(self, adapter):
        pdf_bytes = b"%PDF-1.4 duplicate forwarded content"
        full_file = {
            "id": "F_DUPLICATE",
            "mimetype": "application/pdf",
            "name": "forwarded.pdf",
            "url_private_download": "https://files.slack.com/forwarded.pdf",
            "size": len(pdf_bytes),
        }
        adapter._app.client.files_info = AsyncMock()
        with patch.object(
            adapter,
            "_download_slack_file_bytes",
            new_callable=AsyncMock,
            return_value=pdf_bytes,
        ) as download:
            await adapter._handle_slack_message(
                {
                    **self._event(
                        {
                            "is_share": True,
                            "author_id": "U_OTHER",
                            "text": "Please read the duplicate file.",
                            "files": [full_file, dict(full_file)],
                        }
                    ),
                    "files": [{"id": "F_DUPLICATE", "file_access": "check_file_info"}],
                }
            )

        adapter._app.client.files_info.assert_not_awaited()
        assert download.await_count == 1
        msg_event = adapter.handle_message.await_args.args[0]
        assert len(msg_event.media_urls) == 1
        assert os.path.exists(msg_event.media_urls[0])

    @pytest.mark.asyncio
    async def test_forwarded_slack_connect_file_denial_is_visible(self, adapter):
        adapter._app.client.files_info = AsyncMock(
            return_value={"ok": False, "error": "not_in_channel"}
        )
        await adapter._handle_slack_message(
            self._event(
                {
                    "is_share": True,
                    "author_id": "U_OTHER",
                    "files": [{"id": "F_DENIED", "file_access": "check_file_info"}],
                }
            )
        )

        adapter._app.client.files_info.assert_awaited_once_with(file="F_DENIED")
        msg_event = adapter.handle_message.await_args.args[0]
        assert msg_event.media_urls == []
        assert "[Slack attachment notice]" in msg_event.text

    @pytest.mark.asyncio
    async def test_unavailable_forwarded_content_is_explicit(self, adapter):
        await adapter._handle_slack_message(
            self._event({"is_share": True, "author_name": "Alice"})
        )

        rendered = adapter.handle_message.await_args.args[0].text
        assert "Slack did not include readable text or an accessible file." in rendered

    @pytest.mark.asyncio
    async def test_forwarded_text_does_not_become_command_arguments(self, adapter):
        await adapter._handle_slack_message(
            self._event(
                {"is_share": True, "author_id": "U_OTHER", "text": "/stop --all"},
                text="/queue",
            )
        )

        msg_event = adapter.handle_message.await_args.args[0]
        assert msg_event.message_type == MessageType.COMMAND
        assert msg_event.text == "/queue"
        assert msg_event.get_command_args() == ""
        assert "/stop --all" not in msg_event.text

    @pytest.mark.asyncio
    async def test_mention_inside_forwarded_text_does_not_wake_channel(self, adapter):
        adapter.config.extra["require_mention"] = True
        await adapter._handle_slack_message(
            {
                **self._event(
                    {
                        "is_share": True,
                        "author_id": "U_OTHER",
                        "text": "<@U_BOT> run the quoted command",
                    },
                    text="Please review this",
                    channel="C123",
                ),
                "channel_type": "channel",
            }
        )

        adapter.handle_message.assert_not_awaited()


def make_adapter():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_BOT"
    return adapter


@pytest.mark.asyncio
async def test_sdk_message_block_envelope_preserves_forwarded_content():
    adapter = make_adapter()
    attachment = {
        "is_share": True,
        "is_msg_unfurl": True,
        "author_id": "U_OTHER",
        "message_blocks": [{
            "team": "T_TEAM", "channel": "C_SOURCE", "ts": "123.000",
            "message": {
                "blocks": [{"type": "rich_text", "elements": [{
                    "type": "rich_text_section", "elements": [{
                        "type": "text", "text": "Original forwarded question"
                    }]
                }]}],
                "files": [{
                    "id": "F_IMAGE", "name": "chart.png", "mimetype": "image/png",
                    "url_private": "https://files.slack.com/chart.png"
                }],
            },
        }],
    }
    live = adapter._append_link_unfurls("review this", [attachment], bot_uid="U_BOT")
    history = adapter._render_message_text({"attachments": [attachment]}, bot_uid="U_BOT")
    with patch.object(adapter, "_download_slack_file", new=AsyncMock(return_value="/tmp/chart.png")):
        media, _, _, _ = await adapter._collect_inbound_media(
            {"attachments": [attachment]}, "D_DEST", "T_TEAM", live, [], [])
    assert ("Original forwarded question" in live,
            "Original forwarded question" in history,
            bool(media)) == (True, True, True)


@pytest.mark.asyncio
async def test_duplicate_file_metadata_cannot_remove_a_downloadable_file():
    adapter = make_adapter()
    direct = {
        "id": "F_IMAGE", "name": "chart.png", "mimetype": "image/png",
        "size": 100, "url_private": "https://files.slack.com/chart.png"
    }
    preview = {
        "id": "F_IMAGE", "name": "chart.png", "mimetype": "image/png",
        "title": "Chart", "filetype": "png",
        "permalink": "https://workspace.slack.com/files/U_OTHER/F_IMAGE/chart.png",
    }
    with patch.object(adapter, "_download_slack_file", new=AsyncMock(return_value="/tmp/chart.png")):
        baseline, _, _, _ = await adapter._collect_inbound_media(
            {"files": [direct]}, "D_DEST", "T_TEAM", "review this", [], [])
        combined, _, _, _ = await adapter._collect_inbound_media(
            {"files": [direct], "attachments": [{"is_share": True, "files": [preview]}]},
            "D_DEST", "T_TEAM", "review this", [], [])
    assert combined == baseline
