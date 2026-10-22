# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 18:46
# @Desc    :
import json
from pathlib import Path
from typing import List, Optional

import config
from media_platform.douyin.media import extract_cover_url, extract_image_urls, extract_video_urls
from var import source_keyword_var
from tools.user_hash import anonymize_user_id

from ._store_impl import *

# ----------------------------- 文件后端跨运行去重 -----------------------------
# 语义：
# - 作品键 = (aweme_id, source_keyword)。同一作品命中不同关键词不算重复
#   （source_keyword 是研究信号，保留两行）；同关键词重复运行才算重复，跳过。
# - 评论键 = comment_id（评论行本身不带关键词，纯重复行无信息量）。
# - 种子在首次写入前懒加载 data/douyin/jsonl/ 下已有 contents/comments 文件，
#   之后随写入增量维护；仅文件后端（jsonl/json/csv/excel）生效。
# - db/sqlite/postgres/mongodb 保持原生 upsert 语义（重爬可刷新互动数），不去重。

_FILE_BACKENDS = {"jsonl", "json", "csv", "excel"}
_seen_keys: Optional[set] = None


def _content_key(aweme_id, source_keyword) -> str:
    return f"c:{aweme_id}:{source_keyword}"


def _comment_key(comment_id) -> str:
    return f"m:{comment_id}"


def _load_seen_keys(base: Path = Path("data/douyin/jsonl")) -> set:
    """扫描历史 jsonl 文件装载去重种子。坏行跳过，不中断。"""
    seen = set()
    if not base.exists():
        return seen
    for path in sorted(base.glob("*.jsonl")):
        is_comment_file = "comment" in path.name
        is_content_file = "content" in path.name
        if not (is_comment_file or is_content_file):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if is_comment_file:
                        if row.get("comment_id"):
                            seen.add(_comment_key(row["comment_id"]))
                    elif row.get("aweme_id"):
                        seen.add(_content_key(row["aweme_id"], row.get("source_keyword") or ""))
        except OSError:
            continue
    utils.logger.info(f"[store.douyin.dedup] 历史去重种子加载完成：{len(seen)} 个键（来自 {base}）")
    return seen


def _is_duplicate_and_mark(key: str) -> bool:
    """文件后端下检查并登记；重复返回 True。种子懒加载只在首次调用发生。"""
    global _seen_keys
    if config.SAVE_DATA_OPTION not in _FILE_BACKENDS:
        return False
    if _seen_keys is None:
        _seen_keys = _load_seen_keys()
    if key in _seen_keys:
        return True
    _seen_keys.add(key)
    return False


def reset_dedup_cache() -> None:
    """测试用：清空进程内去重缓存，下次写入重新播种。"""
    global _seen_keys
    _seen_keys = None


class DouyinStoreFactory:
    STORES = {
        "csv": DouyinCsvStoreImplement,
        "db": DouyinDbStoreImplement,
        "postgres": DouyinDbStoreImplement,
        "json": DouyinJsonStoreImplement,
        "jsonl": DouyinJsonlStoreImplement,
        "sqlite": DouyinSqliteStoreImplement,
        "mongodb": DouyinMongoStoreImplement,
        "excel": DouyinExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = DouyinStoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[DouyinStoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()


def _extract_comment_image_list(comment_item: Dict) -> List[str]:
    """
    Extract comment image list

    Args:
        comment_item (Dict): Douyin comment

    Returns:
        List[str]: Comment image list
    """
    images_res: List[str] = []
    image_list: List[Dict] = comment_item.get("image_list", [])

    if not image_list:
        return []

    for image in image_list:
        image_url_list = image.get("origin_url", {}).get("url_list", [])
        if image_url_list and len(image_url_list) > 1:
            images_res.append(image_url_list[1])

    return images_res


def _extract_music_download_url(aweme_detail: Dict) -> str:
    """
    Extract music download URL

    Args:
        aweme_detail (Dict): Douyin video

    Returns:
        str: Music download URL
    """
    music_item = aweme_detail.get("music", {})
    play_url = music_item.get("play_url", {})
    music_url = play_url.get("uri", "")
    return music_url


async def update_douyin_aweme(aweme_item: Dict):
    aweme_id = aweme_item.get("aweme_id")
    source_keyword = source_keyword_var.get()
    if _is_duplicate_and_mark(_content_key(aweme_id, source_keyword)):
        utils.logger.info(
            f"[store.douyin.dedup] 跳过重复作品: aweme_id:{aweme_id}, keyword:{source_keyword}"
        )
        return
    user_info = aweme_item.get("author", {})
    interact_info = aweme_item.get("statistics", {})
    save_content_item = {
        "aweme_id": aweme_id,
        "aweme_type": str(aweme_item.get("aweme_type")),
        "title": aweme_item.get("desc", ""),
        "desc": aweme_item.get("desc", ""),
        "create_time": aweme_item.get("create_time"),
        "creator_hash": anonymize_user_id(user_info.get("uid")),  # 创作者匿名哈希
        "user_id": user_info.get("uid"),  # 创作者原始 uid（本地研究用，勿对外传播）
        "author_homepage": f"https://www.douyin.com/user/{user_info.get('sec_uid')}",  # 号主主页链接
        "nickname": user_info.get("nickname"),  # 用户昵称（原始，未脱敏）
        "ip_location": aweme_item.get("ip_label", ""),  # 作品发布 IP 属地（无则为空）
        "liked_count": str(interact_info.get("digg_count")),
        "collected_count": str(interact_info.get("collect_count")),
        "comment_count": str(interact_info.get("comment_count")),
        "share_count": str(interact_info.get("share_count")),
        "last_modify_ts": utils.get_current_timestamp(),
        "aweme_url": f"https://www.douyin.com/video/{aweme_id}",
        "cover_url": extract_cover_url(aweme_item),
        "video_download_url": (extract_video_urls(aweme_item) or [""])[0],
        "music_download_url": _extract_music_download_url(aweme_item),
        "note_download_url": ",".join(extract_image_urls(aweme_item)),
        "source_keyword": source_keyword,
    }
    utils.logger.info(f"[store.douyin.update_douyin_aweme] douyin aweme id:{aweme_id}, title:{save_content_item.get('title')}")
    await DouyinStoreFactory.create_store().store_content(content_item=save_content_item)


async def batch_update_dy_aweme_comments(aweme_id: str, comments: List[Dict]):
    if not comments:
        return
    for comment_item in comments:
        await update_dy_aweme_comment(aweme_id, comment_item)


async def update_dy_aweme_comment(aweme_id: str, comment_item: Dict):
    comment_aweme_id = comment_item.get("aweme_id")
    if aweme_id != comment_aweme_id:
        utils.logger.error(f"[store.douyin.update_dy_aweme_comment] comment_aweme_id: {comment_aweme_id} != aweme_id: {aweme_id}")
        return
    comment_id = comment_item.get("cid")
    if _is_duplicate_and_mark(_comment_key(comment_id)):
        return
    user_info = comment_item.get("user", {})
    parent_comment_id = comment_item.get("reply_id", "0")
    save_comment_item = {
        "comment_id": comment_id,
        "create_time": comment_item.get("create_time"),
        "aweme_id": aweme_id,
        "content": comment_item.get("text"),
        "creator_hash": anonymize_user_id(user_info.get("uid")),  # 评论者匿名哈希
        "user_id": user_info.get("uid"),  # 评论者原始 uid（本地研究用，勿对外传播）
        "author_homepage": f"https://www.douyin.com/user/{user_info.get('sec_uid')}",  # 评论者主页链接
        "nickname": user_info.get("nickname"),  # 用户昵称（原始，未脱敏）
        "ip_location": comment_item.get("ip_label", ""),  # 评论发布 IP 属地（无则为空）
        "sub_comment_count": str(comment_item.get("reply_comment_total", 0)),
        "like_count": (comment_item.get("digg_count") if comment_item.get("digg_count") else 0),
        "last_modify_ts": utils.get_current_timestamp(),
        "parent_comment_id": parent_comment_id,
        "pictures": ",".join(_extract_comment_image_list(comment_item)),
    }
    utils.logger.info(f"[store.douyin.update_dy_aweme_comment] douyin aweme comment: {comment_id}, content: {save_comment_item.get('content')}")

    await DouyinStoreFactory.create_store().store_comment(comment_item=save_comment_item)


async def save_creator(user_id: str, creator: Dict):
    # 教学版：创作者个人资料(昵称/性别/头像/签名/IP/粉丝数等)不再落库，防骚扰。
    return
