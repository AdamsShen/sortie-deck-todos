"""IM 身份绑定：映射表与一次性短码。

路由层面的集成（/bind、权限、命令分发）在 test_im_router.py 覆盖。
"""

from __future__ import annotations

import time

import pytest

from sortie_deck.im.identity import (
    CODE_LENGTH,
    ImIdentityStore,
    LinkCodeService,
    parse_bind_command,
)


@pytest.fixture()
def identity(tmp_path) -> ImIdentityStore:
    return ImIdentityStore(tmp_path / "im_identities.sqlite")


def test_bind_and_resolve(identity):
    identity.bind(provider="feishu", sender_id="ou_a", user_id="usr_1", username="alice")
    hit = identity.resolve("feishu", "ou_a")
    assert hit is not None
    assert hit.username == "alice"
    assert hit.user_id == "usr_1"


def test_rebind_overwrites(identity):
    identity.bind(provider="feishu", sender_id="ou_a", user_id="usr_1", username="alice")
    identity.bind(provider="feishu", sender_id="ou_a", user_id="usr_2", username="bob")
    assert identity.resolve("feishu", "ou_a").username == "bob"


def test_same_sender_on_different_provider_is_independent(identity):
    identity.bind(provider="feishu", sender_id="u1", user_id="usr_1", username="alice")
    assert identity.resolve("dingtalk", "u1") is None


def test_one_user_can_bind_multiple_providers(identity):
    identity.bind(provider="feishu", sender_id="ou_a", user_id="usr_1", username="alice")
    identity.bind(provider="dingtalk", sender_id="dd_a", user_id="usr_1", username="alice")
    assert len(identity.list()) == 2
    assert {i.provider for i in identity.list()} == {"feishu", "dingtalk"}


def test_unbind(identity):
    identity.bind(provider="feishu", sender_id="ou_a", user_id="usr_1", username="alice")
    assert identity.unbind("feishu", "ou_a") is True
    assert identity.resolve("feishu", "ou_a") is None
    assert identity.unbind("feishu", "ou_a") is False


def test_resolve_empty_sender(identity):
    assert identity.resolve("feishu", "") is None


def test_link_code_roundtrip():
    service = LinkCodeService()
    code, expires_at = service.issue(user_id="usr_1", username="alice")
    assert len(code) == CODE_LENGTH
    assert expires_at > time.time()
    assert service.redeem(code) == ("usr_1", "alice")


def test_link_code_is_single_use():
    service = LinkCodeService()
    code, _ = service.issue(user_id="usr_1", username="alice")
    assert service.redeem(code) is not None
    # 二次兑换必须失败：短码会留在聊天记录里，可复用等于永久后门
    assert service.redeem(code) is None


def test_link_code_case_insensitive():
    service = LinkCodeService()
    code, _ = service.issue(user_id="usr_1", username="alice")
    assert service.redeem(code.lower()) == ("usr_1", "alice")


def test_link_code_expires():
    service = LinkCodeService(ttl=-1)
    code, _ = service.issue(user_id="usr_1", username="alice")
    assert service.redeem(code) is None


def test_link_code_unknown():
    assert LinkCodeService().redeem("ZZZZZZ") is None


def test_parse_bind_command():
    assert parse_bind_command("/bind ABC123") == "ABC123"
    assert parse_bind_command("/BIND abc123") == "ABC123"
    assert parse_bind_command("  /bind   abc123  ") == "ABC123"
    assert parse_bind_command("/bind") == ""
    assert parse_bind_command("/approve") is None
    assert parse_bind_command("hello") is None
