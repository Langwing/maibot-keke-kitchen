# 可可喵的厨房 | Keke Kitchen

> 给可可喵一个"真实"的厨房：食材有数量、做饭会消耗、买菜能补货，再配一个可开关的虚拟买菜钱包。
>
> MaiBot 插件 · id: `local.keke-kitchen` · 版本 1.2.0 · 作者 pankai · MIT 协议
>
> 本文件分两部分：**上半部分给人看**（这是什么、怎么用），**下半部分给后续维护的 AI 看**（架构、约定）。人类读者读到这里就够了，下面挑感兴趣的看。

## 安装

1. 把本仓库放进 MaiBot 的插件目录（git clone 或下载解压均可，目录名随意）：
   ```bash
   cd <你的 MaiBot>/plugins
   git clone https://github.com/Langwing/maibot-keke-kitchen.git
   ```
2. 重启 MaiBot，日志出现「厨房插件已加载」即成功；
3. 管理网页 → 插件管理 → 可可喵的厨房 → 配置，把 `whitelist` 填上你的 QQ 号（**不填谁都用不了**）。

> 🇨🇳 **国内源**：本插件与同系列的「备忘录待办」「衣柜」插件在 Gitee 均有国内镜像仓库，GitHub 访问不便时可从 Gitee 获取（镜像链接待补充）。

---

## 一、给人看的部分

### 它能做什么

| 能力 | 说明 |
|---|---|
| 查厨房 | 你问"今天吃什么""厨房里有什么"，可可喵会打开真实的库存清单回答，不再瞎编 |
| 做饭 | 你说"我想吃饺子"，她自己搭配食材并按常识用量扣减（一顿饺子用 1 斤肉左右，不会把 10 斤全用光） |
| 买菜补货 | 你说"我买了 5 斤肉放冰箱"她登记入库；食材不够时她会提醒你，或自己"去菜市场"买回来 |
| 买菜钱包 | 虚拟记账（初始 500 元）：买菜扣钱、给零花钱加钱、能查流水。**纯记账，不涉及真钱** |

### 使用示例（直接在聊天里说人话即可）

- "可可喵，厨房里有什么吃的？"
- "我想吃玉米萝卜猪肉馅的饺子" → 她做饭、扣食材、告诉你还剩什么
- "我又买了一板鸡蛋 30 个，还有两斤五花肉" → 她登记入库
- "给你 100 块买菜钱" → 她充值并道谢
- "你钱包还剩多少钱？" → 她报余额和最近流水

### 谁能用

白名单制：只有主人和指定的朋友（QQ 号写在设置里）能让她动厨房，其他人问了她会礼貌拒绝。

### 网页后台手动维护（v1.1.0 新增，v1.2.0 改版）

MaiBot 管理网页（浏览器打开）→ 侧边栏插件扩展分组 → **可可喵的厨房** 页面，可以：

- 食材按**分类分区**展示：蔬菜、水果、肉类、蛋奶豆制品、水产海鲜、主食粮油、调味料、长期食品、其他
- 顶部分类筛选条（带数量），点某个分类只看该类
- 深色紧凑卡片布局，每个食材一张小卡片：名称、数量、单位、分类下拉、保存、删除
- 数量为 0 的食材卡片标红提示"已用完"
- 新增食材（可选分类、可勾选"无限常备"），添加后自动跳到该分类
- 查看和直接修正买菜钱余额（差额自动记一笔"网页手动修正"流水）
- 看最近做的饭

改库存优先用这个页面，比在聊天里让 AI 改更精确。**分类字段存在数据里**（ingredients 的 `cat`），可可喵对话侧不受影响；v1.1.0 之前的旧数据加载时会按内置映射自动补分类，无法识别的归"其他"。

### 开关在哪（MaiBot 管理网页 → 插件设置 → 可可喵的厨房）

| 开关 | 作用 |
|---|---|
| 插件总开关 | 整个厨房功能的开/关 |
| wallet_enabled | 虚拟买菜钱包开关。觉得记账太费 token 就关掉，关掉后做饭买菜照常，只是不算钱 |
| reset_inventory_on_next_load | 防翻车开关：改成 true 并重载插件，厨房恢复成初始满库存（钱包和账单保留）。**用完记得改回 false** |
| whitelist / blacklist | 能用厨房的 QQ 号 / 被拉黑的 QQ 号 |

---

## 二、给维护 AI 看的部分

### 基本信息

- 插件 ID：`local.keke-kitchen`（v1.2.0，作者 pankai，MIT 协议）
- 运行环境：MaiBot 1.x + maibot-plugin-sdk 2.x，Python ≥ 3.10
- 运行时数据：MaiBot 数据目录下 `data/plugins/local.keke-kitchen/kitchen_state.json`（重启不丢）

### 文件结构

```
maibot-keke-kitchen/
├── _manifest.json   # 插件清单（capabilities 为空是合法的）
├── plugin.py        # 全部逻辑（单文件）
├── config.toml      # 带中文注释的配置模板（Runner 会自动生成/补齐字段）
├── webui.json       # WebUI 页面声明（custom 模式 + 3 个 API 绑定）
├── webui/index.html # 管理页面（单文件，内联 CSS/JS，经 window.maibot 桥接调 API）
├── pyproject.toml   # 包元信息
├── test_import.py   # 导入自测脚本
├── LICENSE
└── README.md        # 本文件
```

### 架构与数据模型

`plugin.py` 分四层，自上而下：

1. **常量与工具函数**：`UNLIMITED = -1.0`（amount 为 -1 即"无限常备"）；`DEFAULT_INGREDIENTS`（初始 38 种：21 种无限常备的米面调料 + 17 种计数的新鲜食材）；`parse_items()`（食材清单解析）；`fmt_amount()`、`_relative_time()`。
2. **`KitchenConfig`**：配置模型（字段见下表）。
3. **`KitchenStore`**：JSON 持久化。`ingredients: {名称: {amount: float, unit: str}}`；`wallet_balance: float`；`transactions: [{ts, amount, desc}]`（正数充值负数支出）；`meals: [{ts, dish, used: [str], by, cost?}]`。`find()` 支持名称部分匹配（"肉"能对上"猪肉"）。损坏的 state 文件会自动重置为初始清单（钱包保留余额字段）。
4. **`KekeKitchenPlugin`**：主类 + 4 个 `@Tool`。

### WebUI 管理 API 契约（v1.1.0 起）

`@API` 方法只能被本插件 webui.json 声明的页面调用；失败要**抛异常**（宿主不认 `success=false`）。现有 4 个：

| API | 行为 |
|---|---|
| `kitchen_state` | 只读。返回 `{categories: [分类列表], items: [{name, amount, unit, cat}], wallet: {enabled, balance}, meals: [{dish, ts}]}` |
| `kitchen_set_item` | 新增/覆盖单个食材（upsert）。amount=-1 即无限常备；负数（-1 除外）报错；cat 必须在 CATEGORIES 里否则按内置映射/"其他"归类 |
| `kitchen_remove_item` | 按名称整个删除（走 `store.find()` 模糊匹配） |
| `kitchen_set_wallet` | 直接设余额；与旧值的差额自动记一条"网页手动修正余额"流水 |

页面改动只需重载页面；**API 或 webui.json 变化必须重载插件**。前端限制：单文件内联、无 CDN/eval、请求体 64KiB、返回 512KiB、每插件并发 2 / 单次 10 秒。

### 工具契约（改动时保持语义）

| 工具 | 关键行为 |
|---|---|
| `kitchen_query` | 只读。返回全部库存（分"计数/无限"两组）+ 钱包余额（仅开启时）+ 最近 3 顿饭。content 必须可读 |
| `kitchen_cook` | 扣料。不够的按现有量扣到 0 并写进 notes（不失败）；无限常备不扣；`cost_estimate` 只记入 meals，**不扣钱包**（钱在买菜时已付，避免双重扣款——这是明确设计，别改） |
| `kitchen_stock` | 补货/设无限/撤掉食材。`cost` 仅在钱包开启时扣款并记流水。LLM 角色扮演买菜时靠它+cost 完成闭环 |
| `kitchen_wallet` | 充值/支出/查余额。钱包关闭时返回提示而非报错。注意工具描述里已告知"买菜花费走 kitchen_stock 的 cost，不要在这里重复记" |

所有工具：`user_id` 必填 → `_guard()` 统一做（总开关/初始化/黑白名单）检查；返回 `{"success": bool, "content": "...", ...}`，content 是给 LLM 看的中文结果。

### 必须遵守的平台约定（在这台机器人上验证过）

- `@Tool` 用 `visibility="visible"`（LLM 每轮可见；文档里的 `core_tool` 参数是旧写法，此 SDK 用 visibility）
- 处理函数签名 `async def xxx(self, **kwargs)`，从 `kwargs.get()` 取参（含平台注入的 `stream_id` 等）
- 配置模型必须含 `plugin` 节（带 `config_version`），可变默认值用 `default_factory`
- 数据只能写在 `self.ctx.paths.data_dir` 下；本插件不发送消息（content 返回给 LLM 由她说），无需声明 capabilities

### 已踩过的坑

- `parse_items` 的"数量在前"分支（`_NUM_UNIT_HEAD_RE`）单位组**必须用非贪婪 `+?`**：贪婪会把"2斤猪肉"解析成（名称=肉，单位=斤猪）。改这里必须跑容器内自测（见下）。
- config.toml 里 `reset_inventory_on_next_load` 用完要提醒用户改回 false，否则每次重载都清库存。

### 部署 / 改动流程

```bash
# 1. 改完 plugin.py 先做语法+解析自测（不用启动 MaiBot）：
python -m py_compile plugin.py
python -c "import sys; sys.path.insert(0,'.'); import plugin; print(plugin.parse_items('猪肉 1斤，2斤猪肉，米 无限'))"
# 2. 同步到运行环境的 plugins/ 目录，重启 MaiBot（或在管理页面重载插件）
# 3. 验证日志：厨房插件已加载：白名单 N 人，食材 38 种（计数 17 / 常备无限 21），钱包余额 500 元
```

### 待办 / 已知限制

- [ ] 白名单初始为空，等用户提供 QQ 号后写入 config.toml（或用户自己在 WebUI 填）
- [ ] 单位不做换算（"1斤"和"500克"会被当成两笔），依赖 LLM 用词一致，暂可接受
- [ ] 钱包透支允许为负（设计如此：让她能"欠着"再撒娇要零花钱）
- [ ] 无MealPlan/菜谱系统，搭配全靠 LLM 常识——用户明确选择"可可喵自己判断"

### 更新记录

| 版本 | 日期 | 内容 |
|---|---|---|
| 1.2.0 | 2026-10-25 | 管理页改版：食材分类（9 类，存进数据、旧数据自动迁移）、分类筛选条、深色紧凑卡片布局；kitchen_state 返回结构改为 items+categories |
| 1.1.0 | 2026-10-25 | 新增 WebUI 管理页面（webui.json + webui/index.html + 4 个 @API）：网页上看库存、改数量/单位、增删食材、修正钱包余额 |
| 1.0.0 | 2026-10-25 | 初版：4 工具 + 无限常备机制 + 虚拟钱包（带开关）+ 初始清单 38 种 |
