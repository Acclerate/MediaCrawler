# -*- coding: utf-8 -*-
"""
douyin 文件后端跨运行去重（方案B）回归测试。

语义（store/douyin/__init__.py）：
- 作品键 = (aweme_id, source_keyword)：同作品不同关键词不算重复（都保留），
  同关键词重复（跨运行）跳过；
- 评论键 = comment_id：纯重复行跳过；
- 种子懒加载自 data/douyin/jsonl/ 历史文件（测试里用 tmp_path 隔离）；
- 仅文件后端（jsonl/json/csv/excel）生效；db/sqlite/postgres/mongodb 不去重
  （保持 upsert 刷新互动数语义）。
"""
import asyncio
import json
from pathlib import Path

import pytest

import config
import store.douyin as ds
from var import source_keyword_var


class _FakeStore:
    def __init__(self):
        self.contents = []
        self.comments = []

    async def store_content(self, content_item):
        self.contents.append(dict(content_item))

    async def store_comment(self, comment_item):
        self.comments.append(dict(comment_item))


@pytest.fixture()
def isolated_dedup(monkeypatch, tmp_path):
    """每个用例独立：jsonl 后端 + 空种子（不触真实 data/ 目录）+ FakeStore。"""
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    ds.reset_dedup_cache()
    ds._seen_keys = set()  # 预置空种子，跳过懒加载（懒加载行为单独测）
    fake = _FakeStore()
    orig = ds.DouyinStoreFactory.create_store
    ds.DouyinStoreFactory.create_store = staticmethod(lambda: fake)
    yield fake
    ds.DouyinStoreFactory.create_store = orig
    ds.reset_dedup_cache()


def _aweme(aid: str) -> dict:
    return {
        "aweme_id": aid,
        "desc": f"title-{aid}",
        "create_time": 1790000000,
        "author": {"uid": f"u{aid}", "sec_uid": f"sec{aid}", "nickname": "作者"},
        "statistics": {"digg_count": 1, "collect_count": 1, "comment_count": 1, "share_count": 1},
    }


def _comment(aid: str, cid: str) -> dict:
    return {
        "aweme_id": aid,
        "cid": cid,
        "text": f"comment-{cid}",
        "create_time": 1790000001,
        "reply_id": "0",
        "user": {"uid": f"u{cid}", "sec_uid": f"sec{cid}", "nickname": "评论者"},
    }


def test_content_same_keyword_dedup__different_keyword_kept(isolated_dedup):
    source_keyword_var.set("武汉")
    asyncio.run(ds.update_douyin_aweme(_aweme("A1")))
    asyncio.run(ds.update_douyin_aweme(_aweme("A1")))  # 同关键词重复 → 跳过

    source_keyword_var.set("汉口")
    asyncio.run(ds.update_douyin_aweme(_aweme("A1")))  # 不同关键词 → 保留

    assert len(isolated_dedup.contents) == 2
    assert {c["source_keyword"] for c in isolated_dedup.contents} == {"武汉", "汉口"}


def test_comment_dedup_by_comment_id(isolated_dedup):
    source_keyword_var.set("武汉")
    asyncio.run(ds.update_dy_aweme_comment("W1", _comment("W1", "C1")))
    asyncio.run(ds.update_dy_aweme_comment("W1", _comment("W1", "C1")))  # 重复 → 跳过
    asyncio.run(ds.update_dy_aweme_comment("W1", _comment("W1", "C2")))  # 新评论 → 保留
    # 跨关键词重抓同一评论 → 也跳过（评论行不带关键词，纯重复无信息量）
    source_keyword_var.set("汉口")
    asyncio.run(ds.update_dy_aweme_comment("W1", _comment("W1", "C1")))

    assert len(isolated_dedup.comments) == 2
    assert {c["comment_id"] for c in isolated_dedup.comments} == {"C1", "C2"}


def test_dedup_bypassed_for_db_backend(monkeypatch, tmp_path):
    """db 后端不去重：保持 upsert 刷新互动数的原生语义。"""
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "db")
    ds.reset_dedup_cache()
    fake = _FakeStore()
    orig = ds.DouyinStoreFactory.create_store
    ds.DouyinStoreFactory.create_store = staticmethod(lambda: fake)
    try:
        source_keyword_var.set("武汉")
        asyncio.run(ds.update_douyin_aweme(_aweme("A1")))
        asyncio.run(ds.update_douyin_aweme(_aweme("A1")))
    finally:
        ds.DouyinStoreFactory.create_store = orig
        ds.reset_dedup_cache()
    assert len(fake.contents) == 2  # db 模式两行都到 store（由 DB upsert 合并）


def test_seed_loading_from_history_jsonl(tmp_path):
    """历史 jsonl 种子：已有 (aweme_id, keyword) / comment_id 命中即跳过；坏行不炸。"""
    base = tmp_path / "jsonl"
    base.mkdir()
    (base / "search_contents_2026-10-22.jsonl").write_text(
        "\n".join([
            json.dumps({"aweme_id": "OLD1", "source_keyword": "武汉"}, ensure_ascii=False),
            json.dumps({"aweme_id": "OLD2", "source_keyword": ""}, ensure_ascii=False),
            "{bad json line",  # 坏行跳过
            "",
        ]),
        encoding="utf-8",
    )
    (base / "search_comments_2026-10-22.jsonl").write_text(
        json.dumps({"comment_id": "COLD1"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (base / "search_contents_2026-10-22.xlsx").write_text("不是 jsonl，glob 不会命中", encoding="utf-8")

    seen = ds._load_seen_keys(base)
    assert ds._content_key("OLD1", "武汉") in seen
    assert ds._content_key("OLD2", "") in seen
    assert ds._comment_key("COLD1") in seen
    assert len(seen) == 3

    # 用该种子跑写入：旧键跳过、新键落库
    config.SAVE_DATA_OPTION = "jsonl"
    ds._seen_keys = seen
    fake = _FakeStore()
    orig = ds.DouyinStoreFactory.create_store
    ds.DouyinStoreFactory.create_store = staticmethod(lambda: fake)
    try:
        source_keyword_var.set("武汉")
        asyncio.run(ds.update_douyin_aweme(_aweme("OLD1")))  # 命中种子 → 跳过
        asyncio.run(ds.update_douyin_aweme(_aweme("NEW1")))  # 新作品 → 落库
        asyncio.run(ds.update_dy_aweme_comment("X1", _comment("X1", "COLD1")))  # 命中种子 → 跳过
        asyncio.run(ds.update_dy_aweme_comment("X1", _comment("X1", "CNEW")))   # 新评论 → 落库
    finally:
        ds.DouyinStoreFactory.create_store = orig
        ds.reset_dedup_cache()
    assert [c["aweme_id"] for c in fake.contents] == ["NEW1"]
    assert [c["comment_id"] for c in fake.comments] == ["CNEW"]


def test_seed_empty_when_dir_missing(tmp_path):
    assert ds._load_seen_keys(tmp_path / "not_exist") == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
