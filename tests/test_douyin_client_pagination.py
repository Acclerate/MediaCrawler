# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_douyin_client_pagination.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

"""抖音客户端翻页行为的回归测试。

覆盖三个风控/健壮性修复：
1. 创作者作品分页在页与页之间必须限速（此前无 sleep 连续请求会触发风控）；
2. 评论翻页遇到 has_more=1 但空页时必须熔断退出（此前会原地无限重试）；
3. 子评论同样熔断，且 max_count 是顶层评论 + 子评论的总上限。

不发起任何网络请求，get_* 方法全部替换为内存桩。
"""

from __future__ import annotations

import pytest

from media_platform.douyin.client import DouYinClient


class _StubPage:
    async def evaluate(self, expression):  # noqa: ANN001
        return {}


def _make_client() -> DouYinClient:
    return DouYinClient(
        headers={"User-Agent": "test-user-agent", "Cookie": "a=1"},
        playwright_page=_StubPage(),
        cookie_dict={"UIFID": "u", "s_v_web_id": "v"},
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """把 client 模块里的 asyncio.sleep 换成记录器，避免真实等待并统计调用"""
    sleeps: list[float] = []

    async def fake_sleep(interval):
        sleeps.append(interval)

    monkeypatch.setattr("media_platform.douyin.client.asyncio.sleep", fake_sleep)
    return sleeps


# ---------------------------------------------------------------------------
# 1. 创作者分页限速
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creator_posts_sleep_between_pages(no_sleep):
    """has_more=1 时翻页之间必须 sleep；最后一页之后不再 sleep"""
    client = _make_client()
    cursors: list[str] = []

    async def fake_get_user_aweme_posts(sec_user_id, max_cursor=""):
        cursors.append(max_cursor)
        has_more = 1 if max_cursor in ("", "c1") else 0
        next_cursor = {"": "c1", "c1": "c2"}.get(max_cursor, "")
        return {
            "has_more": has_more,
            "max_cursor": next_cursor,
            "aweme_list": [{"aweme_id": f"v{max_cursor or '0'}"}],
        }

    client.get_user_aweme_posts = fake_get_user_aweme_posts

    result = await client.get_all_user_aweme_posts("sec-uid", crawl_interval=2.0)

    assert cursors == ["", "c1", "c2"]
    assert [item["aweme_id"] for item in result] == ["v0", "vc1", "vc2"]
    # 两页之间各 sleep 一次 = 2 次，间隔值与 crawl_interval 一致
    assert no_sleep == [2.0, 2.0]


@pytest.mark.asyncio
async def test_creator_posts_single_page_no_sleep(no_sleep):
    """只有一页（has_more=0）时不 sleep"""
    client = _make_client()

    async def fake_get_user_aweme_posts(sec_user_id, max_cursor=""):
        return {"has_more": 0, "max_cursor": "", "aweme_list": [{"aweme_id": "v1"}]}

    client.get_user_aweme_posts = fake_get_user_aweme_posts

    await client.get_all_user_aweme_posts("sec-uid", crawl_interval=2.0)

    assert no_sleep == []


# ---------------------------------------------------------------------------
# 2. 主评论空页熔断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comments_break_on_empty_page_with_has_more(no_sleep):
    """has_more=1 但 comments=[] 时熔断，不再用同一个 cursor 无限重试"""
    client = _make_client()
    calls: list[int] = []

    async def fake_get_comments(aweme_id, cursor=0):
        calls.append(cursor)
        return {"has_more": 1, "cursor": 0, "comments": []}  # cursor 永不前进

    client.get_aweme_comments = fake_get_comments

    result = await client.get_aweme_all_comments(
        "aweme-1", crawl_interval=0, max_count=10
    )

    assert result == []
    assert calls == [0]  # 只请求了一次，未无限重试


@pytest.mark.asyncio
async def test_comments_empty_page_with_no_more_stops_normally(no_sleep):
    """has_more=0 且空页：正常退出，行为与修复前一致"""
    client = _make_client()
    calls: list[int] = []

    async def fake_get_comments(aweme_id, cursor=0):
        calls.append(cursor)
        return {"has_more": 0, "cursor": 0, "comments": []}

    client.get_aweme_comments = fake_get_comments

    result = await client.get_aweme_all_comments(
        "aweme-1", crawl_interval=0, max_count=10
    )

    assert result == []
    assert calls == [0]


@pytest.mark.asyncio
async def test_comments_sub_comments_break_on_empty_page(no_sleep):
    """子评论 has_more=1 但空页时熔断"""
    client = _make_client()

    async def fake_get_comments(aweme_id, cursor=0):
        if cursor == 0:
            return {
                "has_more": 0,
                "cursor": 0,
                "comments": [{"cid": "c1", "reply_comment_total": 3, "text": "top"}],
            }
        return {"has_more": 0, "cursor": 0, "comments": []}

    async def fake_get_sub_comments(aweme_id, comment_id, cursor=0):
        return {"has_more": 1, "cursor": 0, "comments": []}  # 子评论空页且 has_more=1

    client.get_aweme_comments = fake_get_comments
    client.get_sub_comments = fake_get_sub_comments

    result = await client.get_aweme_all_comments(
        "aweme-1", crawl_interval=0, is_fetch_sub_comments=True, max_count=10
    )

    # 只有顶层那条，子评论空页熔断后不会卡死
    assert [c["cid"] for c in result] == ["c1"]


# ---------------------------------------------------------------------------
# 3. max_count 是顶层 + 子评论的总上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_count_bounds_top_plus_sub_comments(no_sleep):
    """顶层 1 条 + 子评论若干时，总数不超过 max_count"""
    client = _make_client()

    async def fake_get_comments(aweme_id, cursor=0):
        if cursor == 0:
            return {
                "has_more": 0,
                "cursor": 0,
                "comments": [{"cid": "top", "reply_comment_total": 10, "text": "top"}],
            }
        return {"has_more": 0, "cursor": 0, "comments": []}

    async def fake_get_sub_comments(aweme_id, comment_id, cursor=0):
        return {
            "has_more": 1 if cursor < 2 else 0,
            "cursor": cursor + 1,
            "comments": [
                {"cid": f"sub-{cursor}-{i}", "reply_comment_total": 0, "text": "sub"}
                for i in range(5)
            ],
        }

    client.get_aweme_comments = fake_get_comments
    client.get_sub_comments = fake_get_sub_comments

    result = await client.get_aweme_all_comments(
        "aweme-1", crawl_interval=0, is_fetch_sub_comments=True, max_count=8
    )

    assert len(result) == 8
    assert result[0]["cid"] == "top"
    assert all(c["cid"] != "sub-2-0" for c in result[1:])  # 第三页子评论不应再请求


@pytest.mark.asyncio
async def test_sub_comment_loop_stops_when_quota_reached(no_sleep):
    """子评论填满配额后不再翻下一页 / 下一条评论"""
    client = _make_client()
    sub_calls: list[int] = []

    async def fake_get_comments(aweme_id, cursor=0):
        if cursor == 0:
            return {
                "has_more": 0,
                "cursor": 0,
                "comments": [
                    {"cid": "c1", "reply_comment_total": 99, "text": "top1"},
                    {"cid": "c2", "reply_comment_total": 99, "text": "top2"},
                ],
            }
        return {"has_more": 0, "cursor": 0, "comments": []}

    async def fake_get_sub_comments(aweme_id, comment_id, cursor=0):
        sub_calls.append(cursor)
        return {
            "has_more": 0,
            "cursor": 0,
            "comments": [
                {
                    "cid": f"{comment_id}-sub-{i}",
                    "reply_comment_total": 0,
                    "text": "sub",
                }
                for i in range(3)
            ],
        }

    client.get_aweme_comments = fake_get_comments
    client.get_sub_comments = fake_get_sub_comments

    result = await client.get_aweme_all_comments(
        "aweme-1", crawl_interval=0, is_fetch_sub_comments=True, max_count=5
    )

    assert len(result) == 5  # top1 + 3 subs + top2 前一条 sub 即满
    assert len(result) <= 5
