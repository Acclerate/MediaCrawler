# -*- coding: utf-8 -*-
"""阶段五(C3)：规则引擎黄金集回归门（pytest；黄金集缺失时跳过并提示构建）。

运行: python -m pytest tests/test_dating_rules.py -v
黄金集由 tests/build_golden_draft.py 从历史输出生成（gitignored，含真实爬取内容）。
任何 dating_rules.py 的规则改动跑此门：失败输出混淆矩阵定位误伤正则。
"""
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import dating_rules  # noqa: E402

GOLDEN = Path(__file__).parent / "dating_golden"


def load_notes() -> list[dict]:
    p = GOLDEN / "labeled_notes.jsonl"
    if not p.exists():
        pytest.skip(f"黄金集不存在: {p} — 先运行 python tests/build_golden_draft.py")
    return [json.loads(line) for line in p.open(encoding="utf-8")]


def load_comments() -> list[dict]:
    p = GOLDEN / "labeled_comments.jsonl"
    if not p.exists():
        pytest.skip(f"黄金集不存在: {p} — 先运行 python tests/build_golden_draft.py")
    return [json.loads(line) for line in p.open(encoding="utf-8")]


def replay_notes(rules=dating_rules) -> tuple[Counter, list[str]]:
    """回放全部黄金笔记，返回 (混淆矩阵 expected×actual, 首批差异明细)。供测试与变异验证复用。"""
    matrix, problems = Counter(), []
    for rec in load_notes():
        disp, label, basis = rules.judge_note(rec, rec["gender"], rec["home_ip"])
        matrix[(rec["expected"], disp)] += 1
        if disp != rec["expected"] or (rec.get("label_expected") and label != rec["label_expected"]):
            problems.append(f"note {rec['note_id']}: 期望 {rec['expected']}/{rec.get('label_expected', '')}"
                            f" 实际 {disp}/{label}｜title={rec['title'][:24]}｜依据={basis}")
    return matrix, problems


def test_golden_notes_replay():
    matrix, problems = replay_notes()
    n = sum(matrix.values())
    bad = [k for k in matrix if k[0] != k[1]]
    assert not bad, (f"黄金集笔记回放 {len(problems)}/{n} 条不一致，混淆矩阵(expected→actual): "
                     f"{ {f'{a}→{b}': c for (a, b), c in sorted(matrix.items())} }\n" + "\n".join(problems[:10]))


def test_golden_comments_replay():
    bad = []
    for c in load_comments():
        if dating_rules.is_female_selfintro(c["content"]) != c["expected"]:
            bad.append(f"comment {c['comment_id']}: 期望 {c['expected']} | {c['content'][:40]}")
    assert not bad, f"黄金集评论回放 {len(bad)} 条不一致:\n" + "\n".join(bad[:10])


def test_edge_cases():
    """计划要求的边界用例：自称豁免 / 找女生不算豁免 / 软信号仅分档 / IP 剔除 / 探测失败重试。"""
    n = lambda t, d="", ip="湖北", nick="": {"title": t, "desc": d, "nickname": nick, "ip_location": ip}  # noqa: E731
    j = dating_rules.judge_note
    # R1 自称女生豁免（男号）
    assert j(n("女找男 96年"), 2, "湖北")[0:2] == ("keep", "男号自称女")
    assert j(n("本人女 坐标武汉"), 2, "湖北")[0:2] == ("keep", "男号自称女")
    # "找女生"不构成豁免
    assert j(n("想找个女生一起喝下午茶"), 2, "湖北")[0] == "drop"
    assert j(n("男找女 本人90年"), 2, "湖北")[0] == "drop"
    # 男号中性 → 待复核；软信号仅分档不剔除
    assert j(n("武汉 交友 认识一下"), 2, "湖北")[0] == "review"
    assert j(n("身高165 想认识"), 2, "湖北")[0] == "review"
    # 女号 / 文本回退
    assert j(n("随便发发"), 1, "湖北")[0:2] == ("keep", "女(主页)")
    assert j(n("姐妹们看看"), 0, "湖北")[0:2] == ("keep", "女(文本)")
    assert j(n("男找女"), 0, "湖北")[0] == "drop"
    # R3 IP 双重限制
    assert j(n("女找男"), 1, "上海")[0] == "drop"
    assert j(n("女找男"), 1, "")[0] == "keep"
    assert j(n("女找男", ip="广东"), 1, "湖北")[0] == "drop"
    # 阶段三：探测失败 → 待复核重试（不再误杀）
    assert j(n("女找男"), -1, "")[0] == "review"


def test_comment_edge_cases():
    f = dating_rules.is_female_selfintro
    assert f("女生，99年，坐标汉口，有意者私聊") is True
    assert f("本人女 98年 身高165 想认识 →私信") is True
    assert f("男找女 小姐姐私聊") is False          # 找女生视角
    assert f("本人男 96年 想认识") is False          # 男生自称
    assert f("哈哈哈不错") is False                  # 无自荐要素
    assert f("") is False


if __name__ == "__main__":
    m, p = replay_notes()
    print("混淆矩阵(expected→actual):", dict(m))
    for x in p:
        print(" [DIFF]", x)
    sys.exit(1 if p else 0)
