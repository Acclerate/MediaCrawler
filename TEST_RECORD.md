# MediaCrawler 小红书爬取调试与测试记录

> 记录日期：2026-10-12
> 项目：NanmiCoder/MediaCrawler（本地路径 `D:\privategit\github\MediaCrawler`）
> 任务：调试并通过测试——小红书关键词"武汉"、时间范围近一个月、出口 IP 武汉

---

## 一、结论速览

| 项目 | 结果 |
|---|---|
| 关键词"武汉"检索 | ✅ 通过（4 次运行，累计 111 条唯一笔记） |
| 近一个月时间过滤 | ✅ 通过（111/111 均在 30 天内，实际全部为当日发布） |
| 武汉 IP | ✅ 出口 IP 117.151.29.185（湖北武汉移动），未使用代理 |
| 登录态跨会话持久化 | ✅ 修复后通过（两次窗口外复测：21:30 存活 / 23:02 存活，均 >19 分钟死亡窗口） |
| 复测 pong | ✅ 2026-10-12 23:02:11，距上次爬取结束 20 分钟（≥19m OK），result=True |

## 二、环境搭建

- **克隆**：`git clone https://github.com/NanmiCoder/MediaCrawler.git`。本机 git 全局配置了 `http.https://github.com/.proxy=http://127.0.0.1:7897`，该代理未运行导致克隆失败；用 `git -c http.https://github.com/.proxy=` 绕过后直连成功。
- **Python 环境**：conda base（`D:\ProgramData\anaconda3`），Python 3.12.13。
- **补装依赖**（base 环境原有 playwright/httpx/pydantic 等，缺失部分）：`pyexecjs`、`pyhumps`、`aiomysql`、`motor`、`xhshow>=0.2.0`（小红书请求签名库，缺失时 `pong()` 抛 `No module named 'xhshow'`）、`rapidocr-onnxruntime`（图片 OCR）。
- **playwright 浏览器**：base 环境的 playwright 1.52.0 对应 `chromium-1169` 未下载，执行 `python -m playwright install chromium` 补齐。
- **Node.js**：v24.21.0（pyexecjs 运行时依赖），本机已有。

## 三、配置变更（config/）

| 配置项 | 原值 | 新值 | 原因 |
|---|---|---|---|
| `KEYWORDS` | 编程副业,编程兼职 | 武汉 | 测试目标关键词 |
| `ENABLE_CDP_MODE` | True | False | CDP 默认要求用户手动以调试端口启动 Chrome，测试改用标准 playwright 模式 |
| `CRAWLER_MAX_NOTES_COUNT` | 15 | 40 | 每页固定 20 条，40 = 2 页，测试规模 |
| `ENABLE_GET_COMMENTS` | True | False | 测试阶段提速、控制请求量（遵守项目非商业许可的限流要求） |
| `SORT_TYPE`（xhs_config） | popularity_descending | time_descending | 小红书网页端无"一月内"原生筛选项（仅一天/一周/半年），采用"最新排序 + 按发布时间后过滤"方案 |

## 四、代码修改

### 4.1 store/xhs/__init__.py —— 保留号主原始信息

原版出于防骚扰设计对作者信息匿名化（`creator_hash`=sha256(user_id)[:16]，昵称首尾脱敏）。为满足"找到原帖与号主"需求，`local_db_item`（笔记与评论两处）新增字段：

- `user_id`：创作者原始 ID
- `author_homepage`：`https://www.xiaohongshu.com/user/profile/{user_id}`
- `nickname`：改为保存原始昵称（不再脱敏）

jsonl 存储为整包 dict 直写，新字段无损透传；其余存储后端按显式字段取值，自动忽略新增键。

> ⚠️ 数据文件自此含可定位真人的信息，仅供本地研究，勿对外传播。

### 4.2 media_platform/xhs/core.py —— 登录态失效修复（详见 §6）

1. `stealth.min.js` 注入从非持久化分支移出，持久化/CDP 两种上下文均生效；
2. `launch_persistent_context` 移除 `user_agent` 覆盖，浏览器使用原生 UA（Windows Chrome 136，与自身指纹一致）；
3. 页面加载后以 `navigator.userAgent` 同步真实 UA 至 `self.user_agent`，`create_xhs_client` 请求头改用该值（原为硬编码 macOS Chrome 137）。

### 4.3 filter_last_month.py（新增工具脚本）

按发布时间过滤 jsonl，保留近 N 天（默认 30）。要点：输入文件排除 `_last\d+d.jsonl` 后缀（防自噬）；同一 note_id 保留最新一条（jsonl 追加式存储，多次运行叠加）。

## 五、运行记录

| # | 时间 | 触发/目的 | 结果 |
|---|---|---|---|
| 1 | 20:29–20:33 | 首次运行 | 首装依赖不全：xhshow 缺失（ping 时抛错）→ 装包后懒加载恢复；扫码登录成功；爬 31 条。日志 `run_test.log` |
| 2 | 20:50–20:53 | 验证 4.1 补丁 | 又要求扫码（此问题后定位为 §6）；爬 31 条，31/31 含 user_id/author_homepage；第 2 页部分详情被风控跳过。日志 `run_test2.log` |
| 3 | 21:09–21:11 | 验证 4.2 指纹修复 | 再扫码一次；爬 40 条正常结束 |
| 4 | 22:40–22:42 | 武汉 IP + "武汉"正式测试 | **pong=True 零扫码**；爬 40 条，内容全部为武汉本地信息。日志 `run_test4.log` |

数据产出（`data/xhs/jsonl/`）：
- `search_contents_2026-10-12.jsonl`：原始追加数据（run 3 + run 4）
- `search_contents_2026-10-12_last30d.jsonl`：近 30 天过滤 + 去重，**111 条唯一笔记**（发布区间 2026-10-12 18:38 ~ 22:36）
- `backup_search_contents_2026-10-12*.jsonl`：run 2 备份
- `note_images_ocr.jsonl`：图片 OCR 提取结果（RapidOCR，截图/海报类置信度 0.99+）

## 六、登录态（cookie）失效问题排查报告

### 6.1 现象、诊断与根因

**现象**：`SAVE_LOGIN_STATE=True` 且浏览器正常优雅关闭，但每次新进程启动 `pong()` 均返回 False、重新弹二维码。

**诊断过程**（证据链）：
1. `browser_data/xhs_user_data_dir/Default/Network/Cookies`（SQLite）中 `web_session`/`a1`/`webId` 均已正确落盘并在下次启动加载回上下文 → **本地持久化机制无问题**；
2. 独立复现脚本（`diag_cookie.py`）显示 `query_self()` 返回 `code:-104 "您当前登录的账号没有权限访问"`，且页面 DOM 弹出登录二维码 → **会话被小红书服务端撤销**；
3. 两次会话（run 1 登录态、run 2 登录态）均在浏览器关闭后约 15–17 分钟内失效 → 非偶发。

**根因**（两处指纹矛盾触发风控）：
1. 持久化模式下未注入 `stealth.min.js`（原代码只在非持久化分支注入），`navigator.webdriver=true` 等自动化痕迹直接暴露（实测裸启动确认）；
2. UA 伪装自相矛盾：Windows playwright Chromium 被硬编码报 macOS Chrome 126/137，浏览器指纹（Windows、Chrome 136）与 UA 不一致。

**修复**：见 §4.2。

### 6.2 修复验证与 pong 复测

| 复测 | 距上次浏览器关闭 | pong | 说明 |
|---|---|---|---|
| 修复前（run 2 启动） | ~17 分钟 | ❌ -104 | run 1 会话已死 |
| 修复前（diag_cookie） | ~15 分钟 | ❌ -104 | run 2 会话已死 |
| **修复后第一次** | **19 分钟** | ✅ True | API 返回完整账号信息（昵称/粉丝/收藏），2026-10-12 21:30 |
| **修复后第二次（武汉 IP 测试后）** | **20 分钟** | ✅ True | 2026-10-12 23:02:11，距 run 4 结束（22:42:15）20 分钟，query_self 返回"成功"；由 diag_cookie.py 窗口校验通过后自动执行并记录 |

### 6.2.1 时间戳与死亡窗口协议（已固化为工具链）

依据"每次爬取记录时间戳、pong 复测不得低于死亡窗口"的要求：

- **`run_crawl.py`**：main.py 的包装器。每次爬取自动把 `CRAWL start=… end=… duration=… exit=… notes=…` 追加到 `crawl_runs.log`（notes 按本次运行新增行数统计）；
- **`diag_cookie.py`**：登录态体检。执行前强制读取 `crawl_runs.log` 最近一次 CRAWL 的 end 时间，**距其不足 19 分钟（死亡窗口上限）直接拒绝执行**（退出码 2，`--force` 可跳过但会标记 FORCE）；结果以 `PONG time=… since_last_crawl=… result=…` 追加记录；
- **`crawl_runs.log`**：唯一时间戳台账，历史 4 次运行与全部复测已补录（见文件）。

> 死亡窗口依据：修复前两次会话分别在浏览器关闭后 ≤17 分钟、≤15 分钟内被服务端撤销（§6.1）。取上限并留余量为 19 分钟。若后续观察到更长的失效时间，调大 `diag_cookie.py` 中的 `DEATH_WINDOW_MIN`。

### 6.3 兜底方案（若未来仍偶发失效）

- `LOGIN_TYPE="cookie"`：从真实浏览器 F12 导出 cookie 填入 `config.COOKIES`；
- CDP 模式：`ENABLE_CDP_MODE=True`（用本机真实 Chrome 二进制，指纹最佳；`CDP_CONNECT_EXISTING=True` 可直接复用日常 Chrome 的登录态）。

## 七、IP 策略（"如何拥有合理的 IP"）

**IP 合理性的本质**：小红书风控按 IP 质量与稳定性评分。从优到劣：

| 层级 | 类型 | 风控风险 | 说明 |
|---|---|---|---|
| 1 | 本机住宅/移动宽带 | 最低 | 真实用户环境，与账号常用归属地一致。**本机 117.151.29.185（湖北武汉移动）即属此类，是最合理的 IP，无需任何代理** |
| 2 | 住宅代理（residential） | 低 | 真实家庭宽带出口，可按城市定向（如武汉），按流量计费，成本高 |
| 3 | 移动 4G/5G 代理 | 低 | 运营商 NAT，大量真实用户共享同类出口 |
| 4 | 机房/数据中心代理（含云服务器） | 高 | IP 段在黑名单库中，短时间高频访问极易触发验证码/封会话 |

**结论**：
- **2026-10-13 办公环境实测**：出口切至武汉电信/联通双出口企业网（119.98.x 电信段 + 183.94.x 联通段，同城多出口轮换）。归属地仍为武汉，未触发异地风控；今早 10:39–10:55 完整爬取+17 次主页探测即在该网络下跑通。若未来出现会话内 IP 跳变导致的异常，可将代理固定到单一出口。
- 当前场景（单账号、关键词检索、默认限流 2 秒/请求）：**本机 IP 就是最合理的**，不需要也不应该加代理——IP 与账号环境一致比"换 IP"更安全；
- 若未来部署到云服务器：机房 IP 属高危段，反而劣化；要么继续本机跑，要么用住宅代理；
- 若确需其他城市视角（如模拟非武汉用户搜"武汉"）：选支持**城市定向**的住宅代理；本项目内置支持（`config/base_config.py`）：
  - `ENABLE_IP_PROXY=True` 开启
  - `IP_PROXY_PROVIDER_NAME`：`kuaidaili`（快代理）| `wandouhttp`（豌豆HTTP）——需自行注册充值，在 `proxy/providers/` 填入 API 凭证
  - 或 `static` + `STATIC_PROXY_URL="http://user:pass@host:port"` 手填固定代理
- 项目为非商业学习许可：用代理池规避风控做大规模爬取不符合其使用原则，保持现有规模与限流即可。

## 八、交友数据后处理（process_dating.py，2026-10-13 新增）

需求：剔除男生发帖；提取评论区女生自我介绍并记录其主页地址。

**性别判定（权威优先，文本回退）**：作者主页 `userPageData.basicInfo.gender`（实测编码 1=女、2=男、0=未设置，取自 get_creator_info 解析的 `window.__INITIAL_STATE__`）；未设置/获取失败时按标题+正文强男性特征（男找女/本人男等）文本判定，中性内容保留并标记"未知"。结果缓存 `gender_cache.json`（按 user_id，跨运行复用，探测间隔 2 秒）。

**实测**（2026-10-13 00:11 爬取，51 条唯一笔记 / 370 条评论）：
- 保留 24 条（女(主页)16 + 未知 7 + 女(文本)1），剔除男生发帖 27 条
- 过滤有效性强：多篇标题为"女找男/女生相亲"的帖子实际由**男性账号**发布（引流/代发特征），均被正确剔除
- 评论区女生自荐提取 2 条（如"97年女生 164/49 本科 财务 武汉"+其主页链接）

**输出**：`data/xhs/dating_处理结果_<日期>.xlsx`（Sheet1 保留帖子 / Sheet2 女生自荐评论）+ `dating_processed_<日期>.jsonl`。

**设计取舍**：爬取阶段保留全量原始数据（可复核过滤决策），剔除动作发生在后处理导出层——如需爬取即剔除，可将判定结果回写过滤。

## 九、图片文字提取（OCR）

图片直链已在 `image_list` 字段（带 `Referer: https://www.xiaohongshu.com/` 头可访问）。`ocr_images.py` 用 RapidOCR 提取，实测：截图/海报/信息图置信度 0.99–1.00（46 行微信截图全量提取）；手写/艺术字 ~0.75（有错字）；纯照片无文字。批量：`python ocr_images.py 111 5`。媒体文件整包下载可用自带 `ENABLE_GET_MEDIA=True`。

## 十、工具脚本清单

| 脚本 | 用途 |
|---|---|
| `run_crawl.py [main.py参数...]` | **推荐入口**：包装 main.py，自动记录爬取起止时间戳到 crawl_runs.log |
| `diag_cookie.py` | 登录态体检：强制"距上次爬取 ≥19 分钟死亡窗口"校验后执行 pong，结果记录在案 |
| `filter_last_month.py [天数] [输入文件]` | 按发布时间过滤 + 去重，默认近 30 天 |
| `ocr_images.py [笔记数] [每篇图数]` | 下载笔记图片并 OCR（本地缓存不重复下载） |
| `crawl_runs.log` | 时间戳台账：CRAWL/PONG 记录 |
| `process_dating.py` | 交友数据后处理：剔除男生发帖 + 提取评论区女生自荐（含主页）；`--collect-verdicts` 回收人工复核结论 |

## 十一、标准运行流程

```bash
cd /d/privategit/github/MediaCrawler
D:/ProgramData/anaconda3/python.exe run_crawl.py             # 爬取（推荐入口：自动记录时间戳；登录态已持久化，无需扫码）
D:/ProgramData/anaconda3/python.exe filter_last_month.py 30  # 近一月过滤去重
D:/ProgramData/anaconda3/python.exe process_dating.py        # （交友类）剔除男生帖+提取女生自荐
D:/ProgramData/anaconda3/python.exe ocr_images.py 111 5      # （可选）图片文字提取
D:/ProgramData/anaconda3/python.exe diag_cookie.py           # （可选，≥19 分钟后）登录态体检
```

> 许可提醒：本项目为非商业学习用途，应控制请求频率（默认页间 2 秒、笔记间 2 秒），不得大规模爬取或商用。

## 十二、待办问题记录（2026-10-13 用户反馈，今日暂停爬取，下次会话处理）

对 `dating_处理结果_2026-10-13.xlsx` 的三条反馈及排查结论：

### 问题 1：男生主页的帖子应按"内容自称女生"豁免
- **现状**：process_dating.py 对主页 gender=2 一律剔除
- **要求**：主页是男生时，除非帖子内容自称女生（女找男 / 本人女 / XX年女 / 女硕等），否则不记录
- **实现要点**：需区分"自称女生"与"找女生"两类文本——后者（男找女 / 找个女生 / 蹲女生，如"想找个女生一起喝下午茶"）不构成豁免；male(主页) + 自称女生 → 保留并标记"男号自称女"

### 问题 2：输出未与历史抓取帖子去重
- **现状**：多次运行输出重复出现同一批帖子（搜索结果持续命中同一批活跃帖）
- **方案**：建 `data/xhs/dating_seen_history.json` 记录已推送 note_id + 已报告评论者 user_id；每次处理仅输出新增，历史命中在"去重统计"中汇报；种子数据 = backup_组合关键词(48) + dating_processed(24) + 已报告评论者(2)

### 问题 3：帖子和主页 IP 限制在武汉
- **排查结论（已完成）**：
  - 笔记原始响应含 `ip_location` 字段（实测 51/56 湖北，其余上海/广东/江苏/河南/马来西亚各1）——但当前 extractor/store **未落盘**，需补丁（改 store/xhs/__init__.py 增加 ip_location 字段），下次爬取生效
  - 作者主页 `basicInfo.ipLocation` 已随性别探测缓存于 gender_cache.json（55 湖北 / 山东2 / 泰国1 / 江苏河南上海广东各1）
  - 小红书 IP 属地为**省级粒度**（"湖北"），"限制武汉"落地为 ip ∈ {湖北}（或含"武汉"字样）
- **规则**：保留帖需同时满足——作者主页 IP=湖北 且（补丁生效后）笔记自身 ip_location=湖北；女生自荐评论者主页 IP 同样限湖北

### 处理进展（2026-10-13 10:30 会话）
1. ✅ store 补丁完成：笔记/评论 local_db_item 增加 ip_location 字段
2. ✅ process_dating.py v2 完成：自称女生豁免（区分"自称"与"找女生"）/ 历史去重（dating_seen_history.json）/ IP 双重限制（主页+笔记，白名单=湖北/武汉）；输出 4 个 Sheet（新增帖子/女生自荐(新增)/剔除明细/去重与统计）
3. ✅ 历史种子已初始化：54 帖 + 2 评论者
4. ✅ 端到端验证完成（2026-10-13 10:39–10:55，风控恢复后）：69 输入 → 剔除 33（男生未自称 23 + 男生文本 3 + 主页IP外省 7）→ 历史去重 25 → 新增 11（女5/男号自称女3/未知3，笔记IP 11/11 湖北）→ 历史累计 65 帖

### ⚠️ 事故记录：2026-10-13 10:34 风控拦截
- 现象：隔夜会话失效（pong=False）→ 扫码登录成功（web_session 已更新）→ **首个搜索请求即返回 -104 "您当前登录的账号没有权限访问"**，重试 3 次耗尽退出，无新增数据
- 判定：非代码问题（同代码昨 3 次运行全通过）；账号/设备指纹被风控临时标记——34h 内 3 次完整爬取 + 58 次主页探测 + 多次登录
- 处置：立即停止，不再重试（连续重试会加重风控）
- 恢复建议：① 冷却数小时至一天；② 期间在本机 Chrome 用该账号正常浏览小红书（人性化使用记录）；③ 恢复后先跑 diag_cookie.py 体检（注意 19 分钟窗口协议），通过后再小规模爬取；④ 长期降低请求密度：主页探测间隔 2s→4s、每日爬取次数收敛、CRAWLER_MAX_SLEEP_SEC 可调大
- **后续与恢复（10:38）**：用户确认手机端正常（账号未被封）→ 判定被标记的是旧 profile 设备指纹 → 归档 `browser_data/xhs_user_data_dir_flagged_1013`、启用全新 profile（新 a1/webId）→ 重新扫码登录后**首个搜索请求即正常**（10:40:37），风控解除；主页探测间隔已放宽至 4s（v2 引擎实测 17 次探测无异常）
- 教训：设备指纹（a1）是网页端风控主键；触发源最可能是 58 次密集主页探测。后续若再遇 -104：先手机端验证账号 → 归档 profile 换新设备身份 → 降低探测密度

> 本次会话状态：代码/配置全部就绪（补丁+v2 规则引擎+种子），仅差一次成功爬取完成端到端验证。下次运行：`python run_crawl.py && python process_dating.py`。

## 十三、账号风控升级：平台警告与自动化停止（2026-10-13 15:48）

- **事件**：15:23 定时轮完成后约 13 分钟，用户手机端收到平台账号级警告——"不常见的浏览行为，与真人操作习惯不太一致；本次仅警告，多次违规将影响账号权益和流量"
- **与此前 -104 的区别**：前两次是网页端设备指纹被标记（换 profile 即恢复）；这次是**账号本体**被警告（手机端收到通知），说明行为模式被识别
- **诱因评估**：当日 5 轮爬取（00:11/10:39/11:41/15:23）+ 累计约 90 次作者主页探测 + 每轮约 130 次 API 请求，密度远超正常用户
- **处置**：已删除每 3~4 小时的定时任务；所有自动化停止，进入冷却期
- **恢复条件与降密方案**：
  1. 冷却至少数天，期间用手机端正常使用账号（浏览/点赞等真人行为）
  2. 恢复时频率降至 ≤1 次/天，且避开整时段规律
  3. process_dating.py 主页探测改为"仅对已通过其他过滤的帖子作者探测"（当前对全部作者探测，是主要风险源）；PROBE_INTERVAL_SEC 可进一步放大
  4. 每轮规模可减半（每词 20→10，ENABLE_GET_COMMENTS 视需要关闭）
- **结论**：工具链与数据资产完整保留（历史台账/去重/性别缓存），随时可按降密方案恢复；本项目为非商业学习用途，应以低频小规模运行

## 十四、管线优化阶段一+阶段四实施（2026-10-13，依据 docs/dating_pipeline_optimization_plan.md）

### 14.1 阶段一（C2）：规则引擎抽取 → `dating_rules.py`

- 全部正则表（8 组，模块级 `re.compile` 预编译）与 6 个纯函数（`ip_ok`/`self_claims_female`/`text_gender`/`soft_signals`/`is_female_selfintro`/`judge_note`）迁入新文件；`process_dating.py` 仅保留流程编排，`from dating_rules import ip_ok, is_female_selfintro, judge_note`。
- `judge_note` 签名 `(note_dict, gender, home_ip) -> (处置, 标签, 依据)`，不接触缓存与 I/O；`import dating_rules` 无副作用。
- **验收（离线 A/B 对比）**：旧版快照 vs 新版在真实数据（94 笔记：剔除 27 / 待复核 9 / 去重 41）上全管线对比——探测打桩为只读缓存（不启动浏览器、不触网，账号冷却期零风险），输出重定向临时目录。结果：**五个 Sheet 逐格一致 + 运行后 history 一致 + dating_processed jsonl 一致**。
- 顺带修正 docstring 中过时的 Sheet 清单（原文写 4 个 Sheet，实际代码输出 5 个含"待复核"）。

### 14.2 阶段四（C1）：人工复核结论双向闭环

- **schema 升级**：`manual_verdicts.json` 旧 `{uid: "male"}` → 新 `{uid: {"verdict": "female"|"male", "at": "YYYY-MM-DD"}}`；`load_verdicts()` 读取兼容旧格式（现有 4 条旧记录自动迁移），保存时统一落新格式。
- **笔记侧**：verdict 优先于一切规则（R0）——判女 → keep"人工确认女"，判男 → drop"人工剔除"。
- **评论侧**：`cg==2` 但 verdict=female 的评论者不再跳过（标"人工确认女"）；verdict=male 直接跳过。
- **采集入口**：待复核 Sheet 新增 `user_id`、`复核结论(填female/male)` 两列；`python process_dating.py --collect-verdicts [xlsx路径]`（缺省取最新输出）回收结论并打印收录数。
- **验收（合成数据离线，14 项断言全过）**：①待复核填 female → collect → 下轮该帖进 Sheet1"人工确认女"且不再出现在待复核；②判男作者进剔除明细 + 旧格式 male 兼容生效；③主页性别=男但人工判女的评论者出现在 Sheet2（正常评论者不受影响、判男评论者跳过、R2 评论者去重不变）；④collect 保存时旧格式迁移验证。

### 14.3 复核工作流（上线后）

```
python process_dating.py                          # 常规运行，产出待复核 Sheet
# 人工在待复核 Sheet 的"复核结论"列填 female / male（可多行）
python process_dating.py --collect-verdicts       # 回收结论 → manual_verdicts.json
python process_dating.py                          # 下轮运行结论即生效
```

> 阶段二（A 评论级去重）、阶段三（B 探测重试）、阶段五（C3 黄金集）未实施。评分卡结论（2026-10-13 评估）：建议将阶段三提前至阶段二之前，并补入"仅探测过滤后作者"整改项（TEST_RECORD §13），见 docs/dating_pipeline_optimization_plan.md。
