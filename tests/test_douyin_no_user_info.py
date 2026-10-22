# -*- coding: utf-8 -*-
"""
抖音 douyin 存储链路回归测试（本分支政策版）。

本分支(xhs-dating-research)与 xhs 对齐，在 store/douyin 有意保留可定位号主的
原始身份字段，供个人本地研究使用（勿对外传播）：
- user_id          原始 uid
- author_homepage  号主主页链接(由 sec_uid 拼接)
- nickname         原始昵称(未脱敏)
- ip_location      IP 属地(来自接口 ip_label，可能为空)
- creator_hash     匿名哈希同时保留，便于跨数据集对齐

仍不得持久化（教学版沿用）：
- sec_uid/short_user_id/user_unique_id 作为独立键
- 头像 URL(avatar_*)
- 个人签名(signature)

后端行为差异（有意设计，需回归锁定）：
- jsonl/csv/json/excel 文件后端：整包 dict 透传，新字段全保留
- db/sqlite/postgres ORM 后端：_store_impl._filter_orm_fields 按 ORM 列过滤，
  新字段被丢弃但不抛 TypeError（ORM 表未加列）
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import config
import store.douyin as ds
from database import db_session
from database.models import Base, DouyinAweme, DouyinAwemeComment
from store.douyin._store_impl import _filter_orm_fields
from tools.user_hash import anonymize_user_id


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """隔离文件后端去重的模块级状态：每个用例重置并预置空种子，
    避免用例间污染、也避免懒加载读到真实 data/ 数据。"""
    ds.reset_dedup_cache()
    ds._seen_keys = set()
    yield
    ds.reset_dedup_cache()


# 仍禁用字段(键)：不得作为存储 dict 的 key 出现
FORBIDDEN_KEYS = {"sec_uid", "short_user_id", "user_unique_id", "avatar", "user_signature"}


# ----------------------------- mock payload -----------------------------

def _build_aweme_item() -> dict:
    """贴近真实抖音结构的 mock aweme_item。
    含会被拍平的用户字段(sec_uid/short_id/unique_id/avatar/signature)与 ip_label；
    sec_uid 只允许出现在 author_homepage URL 里，其余敏感值必须不落库。"""
    return {
        "aweme_id": "7234567890123456",
        "aweme_type": 0,
        "desc": "这是一个作品描述,同时作为 title",
        "create_time": 1700000000,
        "ip_label": "上海",
        "author": {
            "uid": "9876543210",
            "sec_uid": "MS4wLjABAAAASecretSecUidForTest",
            "short_id": "88877766",
            "unique_id": "creator_unique_abc",
            "nickname": "抖音创作者",
            "avatar_thumb": {"url_list": ["http://x/avatar_thumb.jpg"]},
            "avatar_medium": {"url_list": ["http://x/avatar_medium.jpg"]},
            "signature": "这是创作者个人签名内容",
        },
        "statistics": {
            "digg_count": 100,
            "collect_count": 5,
            "comment_count": 20,
            "share_count": 3,
        },
        "video": {
            "raw_cover": {"url_list": ["", "http://x/cover.jpg"]},
            "origin_cover": {"url_list": ["", "http://x/cover2.jpg"]},
            "play_addr_h264": {"url_list": ["", "http://x/video_h264.mp4"]},
            "play_addr_256": {"url_list": ["", "http://x/video_256.mp4"]},
            "play_addr": {"url_list": ["", "http://x/video.mp4"]},
        },
        "music": {
            "play_url": {"uri": "http://x/music.mp3"},
        },
        "images": [
            {"url_list": ["", "http://x/note_img1.jpg"]},
            {"url_list": ["", "http://x/note_img2.jpg"]},
        ],
    }


def _build_comment_item() -> dict:
    """贴近真实抖音结构的 mock comment_item。aweme_id 须与传入
    update_dy_aweme_comment 的 aweme_id 一致。"""
    return {
        "aweme_id": "7234567890123456",
        "cid": "1111111111",
        "text": "这是一条评论内容",
        "create_time": 1700000001,
        "reply_id": "0",
        "reply_comment_total": 2,
        "digg_count": 5,
        "ip_label": "北京",
        "user": {
            "uid": "555666777",
            "sec_uid": "MS4wLjABBBBCommentSecUid",
            "short_id": "22233344",
            "unique_id": "commenter_unique_xyz",
            "nickname": "评论员小王",
            "avatar_medium": {"url_list": ["http://x/cavatar.jpg"]},
            "signature": "评论员个人签名内容",
        },
        "image_list": [
            {"origin_url": {"url_list": ["", "http://x/cimg.jpg"]}},
        ],
    }


# ----------------------------- 辅助 -----------------------------

class _FakeStore:
    """捕获存储 dict,不真正落库。"""

    def __init__(self):
        self.contents = []
        self.comments = []

    async def store_content(self, content_item):
        self.contents.append(dict(content_item))

    async def store_comment(self, comment_item):
        self.comments.append(dict(comment_item))


def _patch_factory(fake):
    """把 DouyinStoreFactory.create_store 替换为返回 fake,返回原方法用于还原。"""
    orig = ds.DouyinStoreFactory.create_store
    ds.DouyinStoreFactory.create_store = staticmethod(lambda: fake)
    return orig


def _assert_no_forbidden(captured: dict, label: str):
    hit = set(captured.keys()) & FORBIDDEN_KEYS
    assert not hit, f"[{label}] 存储 dict 仍含禁用键: {hit}"


def _assert_raw_values_absent(captured: dict, raw_values, label: str):
    """头像/签名等原始敏感值不得出现在任何存储值里。
    sec_uid 允许出现在 author_homepage URL 中,不参与子串扫描。"""
    leaked = []
    for rv in raw_values:
        if not rv:
            continue
        for k, v in captured.items():
            if isinstance(v, str) and rv in v:
                leaked.append((k, rv))
    assert not leaked, f"[{label}] 存储 dict 中泄漏了原始敏感值: {leaked}"


# ----------------------------- 测试 -----------------------------

def test_douyin_aweme_keeps_research_fields():
    """作品：保留原始 uid/主页/昵称/IP，仍禁头像与签名。"""
    aweme = _build_aweme_item()
    author = aweme["author"]
    raw_sensitive = [
        author["avatar_thumb"]["url_list"][0],
        author["signature"],
    ]

    fake = _FakeStore()
    orig = _patch_factory(fake)
    try:
        asyncio.run(ds.update_douyin_aweme(aweme))
    finally:
        ds.DouyinStoreFactory.create_store = orig

    assert len(fake.contents) == 1
    captured = fake.contents[0]

    # 1. 禁用键不出现
    _assert_no_forbidden(captured, "douyin_aweme")
    # 2. 头像/签名不泄漏
    _assert_raw_values_absent(captured, raw_sensitive, "douyin_aweme")
    # 3. 研究字段按政策保留(原始值)
    assert captured.get("user_id") == author["uid"]
    assert captured.get("nickname") == author["nickname"]
    assert captured.get("author_homepage") == f"https://www.douyin.com/user/{author['sec_uid']}"
    assert captured.get("ip_location") == aweme["ip_label"]
    # 4. creator_hash 保留且与 anonymize_user_id 一致
    assert captured.get("creator_hash") == anonymize_user_id(author["uid"])
    # 5. 内容字段保留
    assert captured.get("desc") == aweme["desc"]
    assert captured.get("title") == aweme["desc"]
    # 6. _extract_* 内容字段正常提取(非空)
    assert captured.get("cover_url") == "http://x/cover.jpg"
    assert captured.get("video_download_url") == "http://x/video_h264.mp4"
    assert captured.get("music_download_url") == "http://x/music.mp3"
    assert "http://x/note_img1.jpg" in captured.get("note_download_url", "")
    # 7. 互动数据拍平
    assert captured.get("liked_count") == "100"
    assert captured.get("collected_count") == "5"
    assert captured.get("comment_count") == "20"
    assert captured.get("share_count") == "3"
    assert captured.get("aweme_url") == f"https://www.douyin.com/video/{aweme['aweme_id']}"


def test_douyin_comment_keeps_research_fields():
    """评论：保留评论者原始 uid/主页/昵称/IP，仍禁头像与签名。"""
    comment = _build_comment_item()
    user = comment["user"]
    raw_sensitive = [
        user["avatar_medium"]["url_list"][0],
        user["signature"],
    ]

    fake = _FakeStore()
    orig = _patch_factory(fake)
    try:
        asyncio.run(ds.update_dy_aweme_comment(comment["aweme_id"], comment))
    finally:
        ds.DouyinStoreFactory.create_store = orig

    assert len(fake.comments) == 1
    captured = fake.comments[0]

    _assert_no_forbidden(captured, "douyin_comment")
    _assert_raw_values_absent(captured, raw_sensitive, "douyin_comment")

    assert captured.get("user_id") == user["uid"]
    assert captured.get("nickname") == user["nickname"]
    assert captured.get("author_homepage") == f"https://www.douyin.com/user/{user['sec_uid']}"
    assert captured.get("ip_location") == comment["ip_label"]
    assert captured.get("creator_hash") == anonymize_user_id(user["uid"])

    # 评论内容/ID 保留
    assert captured.get("content") == comment["text"]
    assert captured.get("comment_id") == comment["cid"]
    assert captured.get("aweme_id") == comment["aweme_id"]
    assert captured.get("parent_comment_id") == "0"
    assert captured.get("sub_comment_count") == "2"
    assert captured.get("pictures") == "http://x/cimg.jpg"


def test_douyin_db_backend_filters_non_orm_fields(monkeypatch):
    """db/sqlite/postgres 后端：_filter_orm_fields 把 ORM 没有的研究字段过滤掉，
    构造 ORM 不抛 TypeError；端到端真实落库回读仍正确。"""
    aweme = _build_aweme_item()

    # ---- 1. 真实 store dict 带新字段 ----
    fake = _FakeStore()
    orig = _patch_factory(fake)
    try:
        asyncio.run(ds.update_douyin_aweme(aweme))
    finally:
        ds.DouyinStoreFactory.create_store = orig
    captured = fake.contents[0]
    assert "user_id" in captured and "author_homepage" in captured

    # ---- 2. 过滤后 ORM 构造不抛异常 ----
    filtered = _filter_orm_fields(captured, DouyinAweme)
    assert "user_id" not in filtered and "author_homepage" not in filtered
    obj = DouyinAweme(**filtered)  # 不抛异常即对得上
    assert obj.aweme_id == aweme["aweme_id"]

    # ---- 3. 端到端：内存 SQLite + 真实 store_content ----
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_session, "get_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "db")

    async def _scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await ds.update_douyin_aweme(aweme)
        async with db_session.get_session() as session:
            res = await session.execute(
                select(DouyinAweme).where(DouyinAweme.aweme_id == aweme["aweme_id"])
            )
            row = res.scalar_one_or_none()
        await engine.dispose()
        return row

    row = asyncio.run(_scenario())

    assert row is not None, "作品未写入 SQLite"
    assert row.title == aweme["desc"]
    assert row.cover_url == "http://x/cover.jpg"
    assert row.video_download_url == "http://x/video_h264.mp4"
    assert row.creator_hash == anonymize_user_id(aweme["author"]["uid"])

    # ORM 无研究字段列（有意：db 后端丢弃新字段）
    orm_cols = {c.name for c in DouyinAweme.__table__.columns}
    assert not (orm_cols & FORBIDDEN_KEYS), f"ORM 仍含禁用列: {orm_cols & FORBIDDEN_KEYS}"
    assert {"user_id", "author_homepage", "ip_location"} - orm_cols

    # ---- 4. 评论同样过滤后可构造 ----
    comment = _build_comment_item()
    fake_c = _FakeStore()
    orig_c = _patch_factory(fake_c)
    try:
        asyncio.run(ds.update_dy_aweme_comment(comment["aweme_id"], comment))
    finally:
        ds.DouyinStoreFactory.create_store = orig_c
    captured_comment = fake_c.comments[0]
    filtered_comment = _filter_orm_fields(captured_comment, DouyinAwemeComment)
    comment_obj = DouyinAwemeComment(**filtered_comment)  # 不抛异常即对得上
    assert comment_obj.comment_id == comment["cid"]
    _assert_no_forbidden(captured_comment, "douyin_comment_orm_construct")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
