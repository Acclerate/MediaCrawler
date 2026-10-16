# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_douyin_search_mix_info.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

"""抖音搜索结果解析的回归测试。

回归背景：search 流程里 `aweme_mix_info` 兼容分支假设该键存在时一定是 dict 且
mix_items 非空，实际上接口会返回 `aweme_mix_info: null` 或 `mix_items: []`，
原来的 `except TypeError` 接不住 AttributeError / IndexError，单条脏数据会击穿
整个 search 流程。解析逻辑现收敛在
``media_platform.douyin.help.parse_aweme_info_from_search_item``。
"""

from __future__ import annotations

from media_platform.douyin.help import parse_aweme_info_from_search_item


def test_normal_aweme_info():
    assert parse_aweme_info_from_search_item({"aweme_info": {"aweme_id": "a1"}}) == {
        "aweme_id": "a1"
    }


def test_mix_info_with_items():
    post_item = {"aweme_mix_info": {"mix_items": [{"aweme_id": "m1"}]}}
    assert parse_aweme_info_from_search_item(post_item) == {"aweme_id": "m1"}


def test_mix_info_none_does_not_raise():
    """aweme_mix_info 显式为 null：修复前抛 AttributeError（except TypeError 接不住）"""
    assert parse_aweme_info_from_search_item({"aweme_mix_info": None}) is None


def test_mix_items_empty_list_does_not_raise():
    """mix_items 为空列表：修复前抛 IndexError"""
    assert (
        parse_aweme_info_from_search_item({"aweme_mix_info": {"mix_items": []}}) is None
    )


def test_mix_info_missing_mix_items_key():
    assert parse_aweme_info_from_search_item({"aweme_mix_info": {}}) is None


def test_both_missing_returns_none():
    assert parse_aweme_info_from_search_item({}) is None


def test_empty_aweme_info_falls_through_to_mix():
    """aweme_info 为空 dict（falsy）时应继续尝试 mix 分支"""
    post_item = {
        "aweme_info": {},
        "aweme_mix_info": {"mix_items": [{"aweme_id": "m1"}]},
    }
    assert parse_aweme_info_from_search_item(post_item) == {"aweme_id": "m1"}
