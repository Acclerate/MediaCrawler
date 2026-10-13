# -*- coding: utf-8 -*-
"""交友数据规则引擎：与流程分离的纯函数与正则表（自 process_dating.py v2 抽取，阶段一/C2）。

本模块不接触缓存与任何 I/O（import 无副作用）：
  judge_note(note_dict, gender, home_ip) -> (处置 keep/review/drop, 标签, 依据)
规则语义与抽取前完全一致，仅做模块级 re.compile 预编译。
"""
import re

WUHAN_IP = ("湖北", "武汉")  # IP 属地白名单（省级粒度）

# R1："自称女生"特征（表明发帖人自己是女生）
FEMALE_SELF_CLAIM = tuple(re.compile(p) for p in (
    r"女找男", r"本人女", r"我是女生", r"我女生", r"本女生",
    r"\d{2}年?女[，,、 ]", r"女生相亲", r"女生[找征]", r"女相亲", r"女硕", r"小仙女", r"姐姐[ ]?\d{2}",
))
# "找女生"类特征（男性视角寻女生，不构成自称豁免）
MALE_SEEK_FEMALE = tuple(re.compile(p) for p in (
    r"男找女", r"找.{0,4}个?女生", r"蹲.{0,3}女生", r"女朋友", r"想?要?个?小姐姐",
))
# 主页未设置性别时的文本回退（强男性特征才剔除）
MALE_STRONG = tuple(re.compile(p) for p in (
    r"男找女", r"本人男", r"男生[，, ]", r"\d{2}年男", r"我男[的生]", r"男研", r"男硕",
))
FEMALE_HINT = tuple(re.compile(p) for p in (
    r"女找男", r"本人女", r"女生[，, ]", r"\d{2}年女", r"姐妹", r"小姐[姐]",
))
# 软性别信号（B 方案）：主页性别不可靠（注册时可能乱选），用内容/昵称辅助分档。
# 只影响分档（进待复核/标注），软信号不单独构成剔除依据。
FEMALE_SOFT = {name: re.compile(pat) for name, pat in {
    "身高150-169": r"(?<!\d)1[5-6]\d(?!\d)",
    "体重40-59": r"体重[:：]?\s*(?<!\d)[4-5]\d(?!\d)|(?<!\d)[4-5]\d(?!\d)\s*(kg|公斤|KG)",
    "裙装/JK/汉服": r"裙|JK|jk|汉服|洛丽塔",
    "女硕": r"女硕",
    "女性化昵称": r"仙女|妹|girl|Girl",
}.items()}
MALE_SOFT = {name: re.compile(pat) for name, pat in {
    "身高175+": r"(?<!\d)(17[5-9]|1[89]\d)(?!\d)",
    "男性化昵称": r"先森|先生|boy|Boy|大兄弟",
}.items()}
# 评论区女生自荐特征：女性标记 + 自我介绍要素
FEMALE_SELFINTRO = tuple(re.compile(p) for p in (
    r"女生", r"本人女", r"女[，,、 ]", r"姐妹", r"姐姐",
))
SELFINTRO_ELEM = tuple(re.compile(p) for p in (
    r"\d{2}年", r"\d{2}岁", r"坐标", r"身高", r"\d{3}cm", r"私[信联]", r"认识", r"有意", r"联系", r"蹲",
))
# 评论区自荐中的男生自称（负向排除）
MALE_INTRO_NEG = tuple(re.compile(p) for p in (
    r"本人男", r"男生[，, ]",
))


def ip_ok(ip: str | None) -> bool:
    return bool(ip) and any(w in str(ip) for w in WUHAN_IP)


def self_claims_female(text: str) -> bool:
    if any(p.search(text) for p in MALE_SEEK_FEMALE):
        return False  # 明确在"找女生"，不算自称
    return any(p.search(text) for p in FEMALE_SELF_CLAIM)


def text_gender(title_desc: str) -> str:
    if any(p.search(title_desc) for p in MALE_STRONG):
        return "male_text"
    if any(p.search(title_desc) for p in FEMALE_HINT):
        return "female_text"
    return "unknown"


def soft_signals(text: str, nickname: str = "") -> tuple[list[str], list[str]]:
    """返回命中的 (女性软信号, 男性软信号) 名称列表。文本=标题+正文，昵称单独匹配女性化/男性化昵称。"""
    f_hits = [name for name, pat in FEMALE_SOFT.items()
              if name != "女性化昵称" and pat.search(text)]
    m_hits = [name for name, pat in MALE_SOFT.items()
              if name != "男性化昵称" and pat.search(text)]
    if nickname:
        if FEMALE_SOFT["女性化昵称"].search(nickname):
            f_hits.append("女性化昵称")
        if MALE_SOFT["男性化昵称"].search(nickname):
            m_hits.append("男性化昵称")
    return f_hits, m_hits


def is_female_selfintro(content: str) -> bool:
    if not content:
        return False
    if any(p.search(content) for p in MALE_SEEK_FEMALE + MALE_INTRO_NEG):
        return False
    has_female = any(p.search(content) for p in FEMALE_SELFINTRO)
    has_intro = any(p.search(content) for p in SELFINTRO_ELEM)
    return has_female and has_intro


def judge_note(note_dict: dict, gender: int, home_ip: str) -> tuple[str, str, str]:
    """返回 (处置, 判定标签, 依据)。处置: keep / review(待复核) / drop。

    A 方案：男号 + 内容中性/女性化软信号 → 待复核，不直接剔除（主页性别注册时可能乱选）。
    B 方案：软信号（身高/体重/服装/昵称）辅助分档，但软信号不单独构成剔除依据。
    """
    text = (note_dict.get("title") or "") + " " + (note_dict.get("desc") or "")
    nickname = note_dict.get("nickname") or ""
    note_ip = note_dict.get("ip_location") or ""
    # R3: IP 双重限制（笔记属地缺失时仅以主页判定，补丁前的旧数据）
    if home_ip and not ip_ok(home_ip):
        return "drop", "", f"主页IP={home_ip}"
    if note_ip and not ip_ok(note_ip):
        return "drop", "", f"笔记IP={note_ip}"
    f_soft, m_soft = soft_signals(text, nickname)
    # R1: 性别规则
    if gender == 1:
        return "keep", "女(主页)", ""
    if gender == 2:
        if self_claims_female(text):
            return "keep", "男号自称女", "内容强自称(女找男/本人女等)"
        if any(p.search(text) for p in MALE_SEEK_FEMALE + MALE_STRONG):
            return "drop", "", "男号+明确男性/找女生特征"
        if f_soft:
            return "review", "待复核(男号+女性化信号)", "+".join(f_soft)
        return "review", "待复核(男号+内容中性)", "主页性别不可靠，需人工判断"
    if gender == -1:
        return "drop", "", "主页获取失败"
    t = text_gender(text)  # gender == 0 未设置
    if t == "male_text":
        return "drop", "", "男生(文本)"
    if t == "female_text":
        return "keep", "女(文本)", ""
    if f_soft:
        return "keep", "女(软信号)", "+".join(f_soft)
    if m_soft:
        return "review", "待复核(未设置+男性化信号)", "+".join(m_soft)
    return "keep", "未知(未设置)", ""
