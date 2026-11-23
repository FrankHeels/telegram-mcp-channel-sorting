import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, User

from telegram_mcp.tools import chats


def _channel(id, title, *, date=None, count=None, archived=False, **flags):
    entity = Channel(
        id=id,
        title=title,
        photo=None,
        date=date,
        broadcast=True,
        participants_count=count,
        **flags,
    )
    return SimpleNamespace(entity=entity, archived=archived)


class DialogClient:
    def __init__(self, dialogs, details=None):
        self.dialogs = dialogs
        self.details = details or {}
        self.requests = []

    async def get_dialogs(self, *, limit, archived):
        # An archived pin can still appear when requesting non-archived dialogs.
        return self.dialogs[:limit]

    async def __call__(self, request):
        assert isinstance(request, GetFullChannelRequest)
        channel_id = request.channel.id
        self.requests.append(channel_id)
        details = self.details[channel_id]
        if isinstance(details, Exception):
            raise details
        return SimpleNamespace(full_chat=details)


@pytest.fixture
def use_dialogs(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    monkeypatch.setenv("TELEGRAM_BLOCKED_CHAT_IDS", "")
    monkeypatch.setattr(chats, "ensure_connected", AsyncMock())

    def setup(dialogs, details=None):
        client = DialogClient(dialogs, details)
        monkeypatch.setattr(chats, "get_client", lambda account=None: client)
        return client

    return setup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sort_order", "expected"),
    [
        ("joined_at", None, ["zeta", "alpha", "Beta"]),
        ("joined_at", "asc", ["alpha", "zeta", "Beta"]),
        ("title", None, ["alpha", "Beta", "zeta"]),
        ("title", "desc", ["zeta", "Beta", "alpha"]),
    ],
)
async def test_sort_all_subscribed_channels_before_limiting(
    use_dialogs, sort_by, sort_order, expected
):
    old = datetime(2022, 1, 1, tzinfo=timezone.utc)
    new = datetime(2025, 1, 1, tzinfo=timezone.utc)
    client = use_dialogs(
        [
            SimpleNamespace(entity=User(id=1, first_name="A user")),
            _channel(2, "left", date=new, left=True),
            _channel(3, "Beta", date=new, min=True),
            _channel(4, "alpha", date=old),
            _channel(5, "zeta", date=new, archived=True),
        ]
    )

    result = json.loads(await chats.list_chats(sort_by=sort_by, sort_order=sort_order, limit=3))

    assert [row["title"] for row in result["results"]] == expected
    assert {row["title"]: row["joined_at"] for row in result["results"]} == {
        "alpha": old.isoformat(),
        "zeta": new.isoformat(),
        "Beta": None,
    }
    assert client.requests == []


@pytest.mark.asyncio
async def test_subscriber_count_reuses_details_and_respects_chat_policy(monkeypatch, use_dialogs):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-1000000000001,-1000000000002,-1000000000003")
    monkeypatch.setenv("TELEGRAM_BLOCKED_CHAT_IDS", "-1000000000003")
    client = use_dialogs(
        [
            _channel(1, "small", count=20),
            _channel(2, "large"),
            _channel(3, "blocked"),
            _channel(4, "not allowed"),
        ],
        {2: SimpleNamespace(participants_count=300, about="A large channel")},
    )

    result = json.loads(
        await chats.list_chats(sort_by="subscriber_count", with_about=True, limit=1)
    )

    (channel,) = result["results"]
    assert (channel["title"], channel["subscriber_count"], channel["about"]) == (
        "large",
        300,
        "A large channel",
    )
    assert client.requests == [2]


@pytest.mark.asyncio
async def test_unavailable_count_is_last_in_ascending_order_and_archive_filter_applies(
    use_dialogs,
):
    client = use_dialogs(
        [
            _channel(1, "unknown"),
            _channel(2, "large", count=400),
            _channel(3, "archived", archived=True),
            _channel(4, "small", count=9),
        ],
        {1: ChannelPrivateError(request=None)},
    )

    result = json.loads(
        await chats.list_chats(sort_by="subscriber_count", sort_order="asc", archived=False)
    )

    assert [row["subscriber_count"] for row in result["results"]] == [9, 400, None]
    assert client.requests == [1]


@pytest.mark.asyncio
async def test_subscriber_count_stops_requesting_details_on_flood_wait(use_dialogs):
    client = use_dialogs(
        [_channel(1, "first"), _channel(2, "second")],
        {1: FloodWaitError(request=None, capture=42)},
    )

    result = await chats.list_chats(sort_by="subscriber_count")

    assert "waiting 42 seconds" in result
    assert client.requests == [1]
