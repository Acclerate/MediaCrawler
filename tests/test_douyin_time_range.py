# -*- coding: utf-8 -*-
"""
douyin 时间范围过滤（CRAWLER_RECENT_DAYS）回归测试。

策略（见 media_platform/douyin/help.py 与 core.py search）：
- 原生档位映射：近 N 天 → 抖音接口唯一支持的三档(1/7/180)中"最小覆盖档"，
  超过半年不设档防漏；
- 排序：开启时切换"最新发布"（LATEST），结果按时间倒序；
- 本地精确过滤：create_time < cutoff 的作品不落库、不抓评论；
- 提前终止：最新排序下整页均越界即停止翻页（不再请求后续页）。
"""
import time

import pytest

import config
import media_platform.douyin.core as dy_core
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.field import PublishTimeType, SearchSortType
from media_platform.douyin.help import (
    native_publish_time_bracket,
    time_range_cutoff_ts,
)

DAY = 86400


# ----------------------------- 纯函数 -----------------------------

def test_native_publish_time_bracket_mapping():
    # 近 N 天 → 最小覆盖档（宁多勿少；超半年不设档防漏）
    assert native_publish_time_bracket(0) == PublishTimeType.UNLIMITED
    assert native_publish_time_bracket(-5) == PublishTimeType.UNLIMITED
    assert native_publish_time_bracket(1) == PublishTimeType.ONE_DAY
    assert native_publish_time_bracket(2) == PublishTimeType.ONE_WEEK
    assert native_publish_time_bracket(7) == PublishTimeType.ONE_WEEK
    assert native_publish_time_bracket(30) == PublishTimeType.SIX_MONTH  # 近一月
    assert native_publish_time_bracket(90) == PublishTimeType.SIX_MONTH  # 近三月
    assert native_publish_time_bracket(180) == PublishTimeType.SIX_MONTH
    assert native_publish_time_bracket(365) == PublishTimeType.UNLIMITED  # 超半年


def test_time_range_cutoff_ts():
    assert time_range_cutoff_ts(0) == 0
    assert time_range_cutoff_ts(-1) == 0
    now = 1_800_000_000.0
    assert time_range_cutoff_ts(30, now=now) == int(now) - 30 * DAY
    assert time_range_cutoff_ts(90, now=now) == int(now) - 90 * DAY
    # 默认取当前时间时结果落在合理窗口内
    assert abs(time_range_cutoff_ts(30) - (int(time.time()) - 30 * DAY)) <= 2


# ----------------------------- search 集成 -----------------------------

class _FakeDyClient:
    """按 offset//10 取预置页，记录每次请求的 publish_time/sort_type。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def search_info_by_keyword(
        self, keyword, offset, publish_time, sort_type, search_id
    ):
        self.calls.append(
            {"offset": offset, "publish_time": publish_time, "sort_type": sort_type}
        )
        idx = offset // 10
        if idx < len(self.pages):
            return self.pages[idx]
        return {"data": [], "extra": {"logid": ""}}


def _aweme(aid: str, ts: int) -> dict:
    return {"aweme_id": aid, "create_time": ts, "desc": f"title-{aid}"}


def _run_search(monkeypatch, fake_client, recent_days, max_notes=20):
    """跑 DouYinCrawler.search()，返回 (落库的作品ID, 抓评论的作品ID列表)。"""
    now = int(time.time())
    monkeypatch.setattr(config, "KEYWORDS", "测试词")
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", max_notes)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "CRAWLER_RECENT_DAYS", recent_days)

    crawler = DouYinCrawler()
    crawler.dy_client = fake_client

    stored = []

    async def fake_update(aweme_item):
        stored.append(aweme_item["aweme_id"])

    monkeypatch.setattr(dy_core.douyin_store, "update_douyin_aweme", fake_update)

    async def fake_media(aweme_item):
        pass

    comment_targets = []

    async def fake_batch_comments(aweme_ids):
        comment_targets.extend(aweme_ids)

    crawler.download_media = fake_media
    crawler.batch_get_note_comments = fake_batch_comments

    import asyncio

    asyncio.run(crawler.search())
    return stored, comment_targets


def test_search_filters_by_recent_days_and_stops_early(monkeypatch):
    now = int(time.time())
    page1 = {
        "data": [
            {"aweme_info": _aweme("new1", now - 5 * DAY)},
            {"aweme_info": _aweme("new2", now - 29 * DAY)},  # 恰在 30 天内
            {"aweme_info": _aweme("old1", now - 40 * DAY)},  # 越界，跳过
        ],
        "extra": {"logid": "abc"},
    }
    page2 = {
        "data": [
            {"aweme_info": _aweme("old2", now - 31 * DAY)},   # 整页越界 → 提前终止
            {"aweme_info": _aweme("old3", now - 100 * DAY)},
        ],
        "extra": {"logid": "abc"},
    }
    fake = _FakeDyClient([page1, page2])

    stored, comment_targets = _run_search(monkeypatch, fake, recent_days=30)

    # 只有窗口内作品落库、进评论批次
    assert stored == ["new1", "new2"]
    assert comment_targets == ["new1", "new2"]
    # 请求参数：最小覆盖档 + 最新排序
    assert fake.calls[0]["publish_time"] == PublishTimeType.SIX_MONTH
    assert fake.calls[0]["sort_type"] == SearchSortType.LATEST
    # 配额允许 2 页，但第 2 页整页越界后提前停止（无第 3 次请求）
    assert len(fake.calls) == 2


def test_search_recent_days_off_keeps_native_config(monkeypatch):
    now = int(time.time())
    page1 = {
        "data": [
            {"aweme_info": _aweme("any1", now - 400 * DAY)},  # 很旧，但不开启过滤就保留
        ],
        "extra": {"logid": "abc"},
    }
    fake = _FakeDyClient([page1])

    stored, comment_targets = _run_search(monkeypatch, fake, recent_days=0, max_notes=10)

    assert stored == ["any1"]
    assert comment_targets == ["any1"]
    # 关闭时沿用 PUBLISH_TIME_TYPE 原值、综合排序
    assert fake.calls[0]["publish_time"] == PublishTimeType(config.PUBLISH_TIME_TYPE)
    assert fake.calls[0]["sort_type"] == SearchSortType.GENERAL
    assert len(fake.calls) == 1


def test_search_recent_days_7_uses_week_bracket(monkeypatch):
    now = int(time.time())
    page1 = {
        "data": [{"aweme_info": _aweme("new1", now - DAY)}],
        "extra": {"logid": "abc"},
    }
    fake = _FakeDyClient([page1])

    stored, _ = _run_search(monkeypatch, fake, recent_days=7, max_notes=10)

    assert stored == ["new1"]
    assert fake.calls[0]["publish_time"] == PublishTimeType.ONE_WEEK


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
