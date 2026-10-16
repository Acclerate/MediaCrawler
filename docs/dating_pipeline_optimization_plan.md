# 交友数据管线优化开发计划

> 分支:`xhs-dating-research` | 制定日期:2026-10-13
> 范围:评论级去重(A)、失败探测可重试(B)、人工复核闭环 + 黄金集回归(C)
> 涉及主线文件:`process_dating.py`、新建 `dating_rules.py`、`tests/` 新增用例

---

## 0. 背景与关键事实

- 现状管线:`run_crawl.py`(爬取)→ `filter_last_month.py`(时间过滤)→ `process_dating.py`(R1 性别 / R2 历史去重 / R3 IP 规则)→ xlsx 多 Sheet 输出。
- 关键事实(决定 A 的设计):爬取阶段按 `time_descending` 关键词搜索时,老帖会再次进入 `search_contents`,其评论会**重新拉取并追加进 `search_comments_*.jsonl`**。因此处理阶段只要扩大评论扫描范围即可获得老帖新自荐,无需新增"回访爬取"环节。
- 现有缺陷对应代码位置:
  - 评论只扫新增帖:`process_dating.py:304`(仅遍历 `kept`)
  - 探测失败永久缓存 `-1` 且直接 drop:`process_dating.py:199-201`、`:239-240`
  - 缓存无 TTL:`gender_cache.json` 全量读写、永不失效
  - 人工台账只支持判男、评论侧不生效:`process_dating.py:270-275`
  - `seed_history()` 每次运行无条件执行、含硬编码路径:`process_dating.py:130-146`、`:257`

---

## 1. 阶段一(C2 前置):规则引擎抽取 —— `dating_rules.py` ✅ 已实施 2026-10-13

> 验收记录见 TEST_RECORD §14.1：真实数据离线 A/B 对比,五 Sheet/历史/jsonl 全一致。

**目标**:规则与流程分离,行为完全不变,为后续改动和黄金集测试提供可导入的纯函数模块。

**改动**:
- 新建 `dating_rules.py`,迁入:
  - 全部正则表(`FEMALE_SELF_CLAIM` / `MALE_SEEK_FEMALE` / `MALE_STRONG` / `FEMALE_HINT` / `FEMALE_SOFT` / `MALE_SOFT` / `FEMALE_SELFINTRO` / `SELFINTRO_ELEM`),模块级 `re.compile` 预编译;
  - 纯函数:`self_claims_female` / `text_gender` / `soft_signals` / `is_female_selfintro` / `ip_ok` / `judge_note`;
  - `judge_note` 签名保持纯函数:`(note_dict, gender: int, home_ip: str) -> (处置, 标签, 依据)`,不接触缓存与 I/O。
- `process_dating.py` 删除本地规则定义,改为 `from dating_rules import ...`。

**验收**:
- 用当天数据各跑一次新旧版本,`dating_处理结果_*.xlsx` 五个 Sheet 行数与内容一致(输出文件名时间戳除外);
- `python -c "import dating_rules"` 无副作用(不触发浏览器/文件写)。

**风险**:低。纯搬迁。

---

## 2. 阶段二(A):评论级去重

**目标**:女生自荐线索的检测独立于"帖子是否已推送"。

**改动**(`process_dating.py`):
1. 自荐扫描入口从 `kept` 扩展为**全部合规帖** = `kept + deduped + reviewed`(即 `judge_note` 处置非 drop、且未被人工判男的帖)。
2. 评论者去重逻辑不变:`uid ∈ history["commenters"]` 则跳过——同一评论者全局只报一次,与所在帖推送状态无关。
3. Sheet4 统计新增行:"来自已推旧帖的自荐数",便于识别 Sheet1 为空但 Sheet2 有产出属正常。

**历史 schema**:不变(`commenters` 已是 `uid → nickname`),无需迁移。

**验收**:
- 构造用例:某已推送帖(`note_id ∈ seen_notes`)的评论 jsonl 中追加一条新自荐评论 → 运行后该评论出现在 Sheet2,`dating_seen_history.json` 的 `commenters` 新增该 uid;
- 同一评论者在多个帖自荐 → 只报一次。

**风险**:低。Sheet2 输出量预期上升(旧帖新自荐首次被扫出),属修复而非回归。

---

## 3. 阶段三(B):失败探测可重试 + 熔断 + TTL

> **前置整改已完成 2026-10-16**（TEST_RECORD §15）：探测后置——Stage A 免探测预过滤
> (台账/历史已见/笔记IP)→ 仅对幸存作者探测；冷缓存实测 94→18 个探测(省 80%)。
> 阶段三其余项(1-7、9)待实施；第 8 项"作者与评论者合并一次浏览器启动"已随 ProbeSession 完成。

**目标**:探测失败不再造成数据永久误杀。

**改动**(`process_dating.py` + `dating_rules.py`):
1. **缓存结构升级**:`gender_cache.json` 值增加 `probed_at`(epoch 秒):
   `{"gender": int, "ip": str, "nickname": str, "probed_at": int}`。
2. **成功才落缓存**:接口异常 → 不写缓存,下次运行自动重探;`result.success=False` 一律不落缓存(无法可靠区分"用户不存在",保守处理;后续确认 code 语义后可加 `-2 不存在` 永久负缓存)。
3. **TTL = 30 天**:`todo` 筛选条件为 uid 缺失或 `probed_at` 超 30 天。
4. **熔断**:单次运行连续 3 个探测异常 → 中止剩余探测并打印告警(疑似风控,继续硬打会烧 cookie)。
5. **`judge_note` 语义调整**:`g == -1` 或 uid 不在缓存 → `("review", "待复核(主页获取失败将重试)")`,不再 drop;review 不进历史,下次自动重试(复用现有机制)。
6. **滞留保护**:`history` 新增 `"probe_fail_count": {uid: n}`;熔断/异常时 +1,成功探测时清除;n ≥ 3 → 降级 drop("主页多次获取失败"),防止永久滞留 review。
7. 清理 `seed_history()`:仅 `--seed` 时执行,主流程不再每次重扫备份文件;硬编码已报告评论者种子移入一次性种子逻辑。
8. `probe_genders` 合并调用:作者与评论者一次启动浏览器完成(现状两次 launch),client 复用。

**验收**:
- 断网/改坏 cookie 跑一次:失败 uid 进 Sheet"待复核"且缓存中**无** `-1` 记录;恢复后重跑自动补探;
- 人为制造连续 3 次异常:探测中止而非继续全量硬打;
- 同一评论者与作者重复出现时不重复探测(共用缓存生效)。

**风险**:中。drop→review 的迁移会使待复核量上升、Sheet1 新增量短期可能增加(误杀被救回),需人工消化一轮。

---

## 4. 阶段四(C1):人工复核结论双向闭环 ✅ 已实施 2026-10-13

> 验收记录见 TEST_RECORD §14.2:合成数据 14 项断言全过(三条验收场景+旧格式迁移)。
> 备注:实施早于阶段二/三——2026-10-13 评分卡结论为"阶段一(87.6)与阶段四(76.1)属推荐档,
> 阶段三(74.2)建议提前到阶段二(61.4)之前实施"。

**目标**:人工判断结果以最高优先级回流规则,笔记侧与评论侧都生效。

**改动**:
1. **schema 升级** `manual_verdicts.json`:
   旧:`{uid: "male"}` → 新:`{uid: {"verdict": "female"|"male", "at": "YYYY-MM-DD"}}`;
   加载函数兼容旧格式并在保存时迁移。
2. **应用点**:
   - 笔记阶段:verdict 优先于一切规则——判女 → keep(标签"人工确认女"),判男 → drop(沿用现有"人工剔除");
   - 评论阶段:`cg == 2` 但 verdict 为 female 的评论者不再被跳过;verdict male 的评论者直接跳过。
3. **采集入口**:"待复核" Sheet 增加两列:`user_id`、`复核结论(填female/male)`;
   新增子命令 `python process_dating.py --collect-verdicts [xlsx路径]`:读最新(或指定)xlsx 待复核 Sheet,把非空结论回写 `manual_verdicts.json`,并打印本次收录数。

**验收**:
- 待复核行填 `female` → 执行 collect → 下次运行该帖进 Sheet1"新增帖子"(标"人工确认女"),不再出现在待复核;
- 填 `male` → 进剔除明细;
- 某评论者主页性别为男但人工判女 → 其自荐评论出现在 Sheet2。

**风险**:低。

---

## 5. 阶段五(C3):黄金标注集 + 回归测试

**目标**:规则改动可量化验证,防止正则调整引起隐性退化。

**改动**:
1. **黄金集**:
   - `tests/dating_golden/labeled_notes.jsonl`:`{title, desc, nickname, gender, home_ip, note_ip, expected: keep|review|drop, label_expected?: str}`
   - `tests/dating_golden/labeled_comments.jsonl`:`{content, expected: true|false}`
   - 规模目标:笔记 ≥ 60 条(keep/drop/review 三档均衡,覆盖"自称女豁免""找女生不算豁免""IP 剔除"边界)、评论 ≥ 30 条。
   - **半自动构建**:新增脚本 `tests/build_golden_draft.py`,从现有 `dating_处理结果_*.xlsx` 反推:新增/剔除 Sheet 行直接取为 keep/drop 草稿(需人工抽查),待复核 Sheet 行全部需人工标 expected;草稿生成后由人工修正定稿。
2. **回归用例** `tests/test_dating_rules.py`(pytest):
   - 逐条回放 `dating_rules.judge_note` / `is_female_selfintro`,断言与 `expected` 严格一致;
   - 失败时打印混淆矩阵(各 expected 档的实际分布),定位是哪条正则误伤;
   - 附带既有边界用例:男号+"女找男"→ keep、男号+"蹲一个女生"→ drop、软信号仅分档不剔除等。

**验收**:
- `pytest tests/test_dating_rules.py` 全绿;
- 在 `dating_rules.py` 故意加一条错误正则(如把 `男找女` 删掉)→ 对应用例失败,证明门是有效的。

**风险**:无(纯新增)。人力成本主要在黄金集人工标注一轮。

---

## 6. 实施顺序与依赖

```
阶段一(规则抽取) ──→ 阶段二(A 评论去重) ──→ 阶段三(B 探测重试)
                └──→ 阶段四(C1 verdict) ──→ 阶段五(C3 黄金集,依赖一+四定稿)
```

| 顺序 | 阶段 | 涉及文件 | 预估工作量 |
|---|---|---|---|
| 1 | 一 规则抽取 | `dating_rules.py`(新)、`process_dating.py` | 0.5 天 |
| 2 | 二 评论去重 | `process_dating.py` | 0.5 天 |
| 3 | 三 探测重试 | `process_dating.py`、`dating_rules.py` | 1 天 |
| 4 | 四 verdict 闭环 | `process_dating.py` | 0.5 天 |
| 5 | 五 黄金集 | `tests/` 4 个新文件 + 人工标注 | 0.5 天 + 人工标注 |

每阶段完成后:当日数据实跑一次,对比 Sheet 行数与预期;阶段五完成后将 `pytest` 作为规则改动的固定回归门。

## 7. 暂不纳入本期的事项(备忘)

- 历史存储 JSON → SQLite(当前数据量下 JSON 尚可控,待 `probe_fail_count` 等字段增多后再迁);
- 线索状态机(new/pushed/contacted/closed)、评分排序输出(Top-N)——待复核闭环跑顺后再评估;
- 探测间隔随机抖动(本期已以熔断兜底);
- 每日 cron 调度与文件锁。
