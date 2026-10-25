"""麦麦的厨房插件。

给麦麦一个"真实"的厨房：食材有数量、做饭会消耗、买菜能补货，
再配一个可开关的虚拟买菜钱包（记账用，不涉及真钱）。

提供给 LLM 的四个工具：
- kitchen_query   查看厨房里有什么食材、买菜钱余额
- kitchen_cook    做饭，按常识用量扣减食材（已登记的成品直接扣成品）
- kitchen_stock   进货/补充/清理食材，做好的成品食物（如包好的饺子）也登记入库
- kitchen_wallet  买菜钱的充值、支出、查账

米、面、调料等常备物资记为"无限"，不参与数量计算，避免无意义的记账。
数据保存在插件数据目录的 kitchen_state.json，重启不丢失。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from maibot_sdk import API, MaiBotPlugin, Tool
from maibot_sdk.config import Field, PluginConfigBase
from maibot_sdk.types import ToolParameterInfo, ToolParamType

logger = logging.getLogger(__name__)

UNLIMITED = -1.0  # amount == -1 表示无限常备

# 食材分类（网页管理与展示用；工具侧不强制分类，由 LLM 补货时不填则归"其他"）
CAT_VEGETABLE = "蔬菜"
CAT_FRUIT = "水果"
CAT_MEAT = "肉类"
CAT_EGG_MILK = "蛋奶豆制品"
CAT_SEAFOOD = "水产海鲜"
CAT_STAPLE = "主食粮油"
CAT_SEASONING = "调味料"
CAT_LONGTERM = "长期食品"
CAT_OTHER = "其他"
CATEGORIES: list[str] = [
    CAT_VEGETABLE,
    CAT_FRUIT,
    CAT_MEAT,
    CAT_EGG_MILK,
    CAT_SEAFOOD,
    CAT_STAPLE,
    CAT_SEASONING,
    CAT_LONGTERM,
    CAT_OTHER,
]

# 初始厨房清单：名称 -> (数量, 单位, 分类)；数量为 -1 表示无限
DEFAULT_INGREDIENTS: dict[str, tuple[float, str, str]] = {
    # 常备主食（无限）
    "大米": (UNLIMITED, "袋", CAT_STAPLE),
    "面粉": (UNLIMITED, "袋", CAT_STAPLE),
    "挂面": (UNLIMITED, "包", CAT_STAPLE),
    "食用油": (UNLIMITED, "桶", CAT_STAPLE),
    "饺子皮": (60, "张", CAT_STAPLE),
    # 常备调料（无限）
    "盐": (UNLIMITED, "包", CAT_SEASONING),
    "白糖": (UNLIMITED, "包", CAT_SEASONING),
    "生抽": (UNLIMITED, "瓶", CAT_SEASONING),
    "老抽": (UNLIMITED, "瓶", CAT_SEASONING),
    "醋": (UNLIMITED, "瓶", CAT_SEASONING),
    "料酒": (UNLIMITED, "瓶", CAT_SEASONING),
    "蚝油": (UNLIMITED, "瓶", CAT_SEASONING),
    "香油": (UNLIMITED, "瓶", CAT_SEASONING),
    "胡椒粉": (UNLIMITED, "瓶", CAT_SEASONING),
    "花椒粉": (UNLIMITED, "瓶", CAT_SEASONING),
    "八角": (UNLIMITED, "包", CAT_SEASONING),
    "干辣椒": (UNLIMITED, "包", CAT_SEASONING),
    "豆瓣酱": (UNLIMITED, "瓶", CAT_SEASONING),
    "甜面酱": (UNLIMITED, "瓶", CAT_SEASONING),
    "淀粉": (UNLIMITED, "包", CAT_SEASONING),
    "鸡精": (UNLIMITED, "包", CAT_SEASONING),
    "蜂蜜": (UNLIMITED, "瓶", CAT_SEASONING),
    # 新鲜食材（计数）
    "猪肉": (3, "斤", CAT_MEAT),
    "肉丸": (20, "个", CAT_MEAT),
    "鸡蛋": (15, "个", CAT_EGG_MILK),
    "牛奶": (1, "升", CAT_EGG_MILK),
    "豆腐": (1, "盒", CAT_EGG_MILK),
    "白菜": (1, "颗", CAT_VEGETABLE),
    "白萝卜": (2, "根", CAT_VEGETABLE),
    "胡萝卜": (2, "根", CAT_VEGETABLE),
    "玉米": (3, "根", CAT_VEGETABLE),
    "土豆": (5, "个", CAT_VEGETABLE),
    "西红柿": (4, "个", CAT_VEGETABLE),
    "青椒": (5, "个", CAT_VEGETABLE),
    "洋葱": (2, "个", CAT_VEGETABLE),
    "大蒜": (3, "头", CAT_VEGETABLE),
    "生姜": (2, "块", CAT_VEGETABLE),
    "小葱": (1, "把", CAT_VEGETABLE),
}

# 初始清单的分类映射，用于给旧数据（没有分类字段）补分类
_DEFAULT_CATEGORY: dict[str, str] = {
    name: cat for name, (_, _, cat) in DEFAULT_INGREDIENTS.items()
}

_NUM_UNIT_TAIL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([^\d\s，,、;；]*)$")
_NUM_UNIT_HEAD_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([^\d\s，,、;；]+?)(.+)$")


def fmt_amount(amount: float) -> str:
    """把 3.0 显示成 3，把 1.5 显示成 1.5。"""
    if float(amount).is_integer():
        return str(int(amount))
    return f"{amount:g}"


def parse_items(spec: str) -> tuple[list[tuple[str, float, str]], list[str]]:
    """解析"猪肉 1斤，鸡蛋 3个"这类清单。

    支持"名称 数量单位""名称:数量单位""数量单位名称"三种写法，
    数量写"无限"表示常备不限量。返回 (解析结果, 解析失败的片段)。
    """
    items: list[tuple[str, float, str]] = []
    bad: list[str] = []
    for seg in re.split(r"[，,、;；\n]+", spec or ""):
        seg = re.sub(r"\s+", " ", seg).strip()
        if not seg:
            continue
        if "无限" in seg or "∞" in seg:
            name = re.sub(r"无限|∞", "", seg).strip(" :：-－")
            if name:
                items.append((name, UNLIMITED, "无限"))
            else:
                bad.append(seg)
            continue
        m = _NUM_UNIT_TAIL_RE.search(seg)
        if m:
            name = seg[: m.start()].strip(" :：-－")
            if name:
                items.append((name, float(m.group(1)), m.group(2)))
                continue
        m2 = _NUM_UNIT_HEAD_RE.match(seg)
        if m2 and m2.group(3).strip():
            items.append((m2.group(3).strip(" :：-－"), float(m2.group(1)), m2.group(2)))
            continue
        bad.append(seg)
    return items, bad


def _relative_time(ts: float) -> str:
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"


class _PluginSection(PluginConfigBase):
    """插件元信息配置节。"""

    config_version: str = "1.0.0"


class KitchenConfig(PluginConfigBase):
    """厨房插件配置。"""

    plugin: _PluginSection = Field(default_factory=_PluginSection)
    enabled: bool = Field(default=True, description="插件总开关")
    whitelist: list[str] = Field(
        default_factory=list,
        description="可以使用厨房的用户 QQ 号列表；为空时无人可用",
    )
    blacklist: list[str] = Field(
        default_factory=list,
        description="禁止使用厨房的用户 QQ 号列表，优先于白名单",
    )
    wallet_enabled: bool = Field(
        default=True,
        description="虚拟买菜钱包开关；关闭后不记账不扣钱，省 token",
    )
    wallet_initial_balance: float = Field(
        default=500.0, ge=0, description="钱包初始余额（元），仅首次创建时生效"
    )
    max_meals_log: int = Field(
        default=50, ge=5, le=500, description="保留最近多少顿做饭记录"
    )
    max_transactions: int = Field(
        default=100, ge=10, le=1000, description="保留最近多少条钱包流水"
    )
    reset_inventory_on_next_load: bool = Field(
        default=False,
        description="临时开关：设为 true 并重载插件，厨房会恢复成初始清单（钱包和账单保留）",
    )


class KitchenStore:
    """厨房状态的 JSON 持久化存储。"""

    def __init__(self, path: Path, initial_balance: float) -> None:
        self._path = path
        self._initial_balance = initial_balance
        # name -> {"amount": float, "unit": str}，amount 为 -1 表示无限
        self.ingredients: dict[str, dict[str, Any]] = {}
        self.wallet_balance: float = initial_balance
        self.transactions: list[dict[str, Any]] = []
        self.meals: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self._path.exists():
            self.reset_inventory()
            self.save()
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("厨房状态文件损坏，已重置为初始清单：%s", self._path)
            self.reset_inventory()
            self.save()
            return
        ingredients = data.get("ingredients")
        if isinstance(ingredients, dict) and ingredients:
            self.ingredients = {}
            for name, info in ingredients.items():
                try:
                    amount = float(info.get("amount", 0))
                    unit = str(info.get("unit", "") or "")
                except (AttributeError, TypeError, ValueError):
                    continue
                cat = str(info.get("cat", "") or "")
                if cat not in CATEGORIES:
                    cat = _DEFAULT_CATEGORY.get(str(name), CAT_OTHER)
                self.ingredients[str(name)] = {"amount": amount, "unit": unit, "cat": cat}
        else:
            self.reset_inventory()
        try:
            self.wallet_balance = float(data.get("wallet_balance", self._initial_balance))
        except (TypeError, ValueError):
            self.wallet_balance = self._initial_balance
        txs = data.get("transactions")
        self.transactions = [t for t in txs if isinstance(t, dict)] if isinstance(txs, list) else []
        meals = data.get("meals")
        self.meals = [m for m in meals if isinstance(m, dict)] if isinstance(meals, list) else []

    def save(self) -> None:
        payload = {
            "ingredients": self.ingredients,
            "wallet_balance": self.wallet_balance,
            "transactions": self.transactions[-1000:],
            "meals": self.meals[-1000:],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.exception("厨房状态保存失败：%s", self._path)

    def reset_inventory(self) -> None:
        """恢复初始清单（不动钱包和账单）。"""
        self.ingredients = {
            name: {"amount": amount, "unit": unit, "cat": cat}
            for name, (amount, unit, cat) in DEFAULT_INGREDIENTS.items()
        }

    # ------------------------------------------------------------------
    # 查询与变动
    # ------------------------------------------------------------------

    def find(self, name: str) -> str | None:
        """按名称找食材：先精确，再做受限的部分匹配。

        允许的模糊匹配：简短叫法落在稍长的食材上（"肉"→"猪肉"）、
        带修饰词的叫法（"新鲜猪肉"→"猪肉"）、成品名只差个括号后缀
        （"玉米萝卜肉馅饺子"→"玉米萝卜肉馅饺子（生）"）。
        不允许：把很长的新名字吸并到只包含其中两三个字的原料上，
        否则登记"玉米萝卜肉馅饺子（生） 60个"会被记到"玉米"头上。
        """
        if name in self.ingredients:
            return name
        if len(name) <= 4:
            # 短叫法：只落在比它长不多的食材上（肉→猪肉；但"玉米"不会落到"玉米萝卜肉馅饺子（生）"）
            for key in sorted(self.ingredients, key=len):
                if name in key and len(key) <= len(name) + 3:
                    return key
        # 长叫法：只匹配几乎同名的食材（新鲜猪肉→猪肉 / 去掉括号后缀的成品名）
        for key in sorted(self.ingredients, key=len):
            if key in name and len(key) >= len(name) - 2:
                return key
            if len(name) >= 5 and name in key and len(key) <= len(name) + 4:
                return key
        return None

    def describe(self, name: str) -> str:
        info = self.ingredients.get(name)
        if info is None:
            return name
        if info["amount"] < 0:
            return f"{name}（常备不限量）"
        if info["amount"] <= 0:
            return f"{name}（用完了）"
        return f"{name} {fmt_amount(info['amount'])}{info['unit'] or ''}"

    def add_transaction(self, amount: float, desc: str) -> None:
        self.transactions.append({"ts": time.time(), "amount": amount, "desc": desc})
        self.transactions = self.transactions[-1000:]

    def add_meal(self, meal: dict[str, Any], limit: int) -> None:
        self.meals.append(meal)
        self.meals = self.meals[-max(limit, 5):]


class MaiKitchenPlugin(MaiBotPlugin):
    """麦麦的厨房插件主类。"""

    config_model = KitchenConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: KitchenStore | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        self._store = KitchenStore(
            data_dir / "kitchen_state.json", float(self.config.wallet_initial_balance)
        )
        self._store.load()
        if self.config.reset_inventory_on_next_load:
            self._store.reset_inventory()
            self._store.save()
            logger.info("厨房已按配置恢复为初始清单（请记得把恢复开关改回 false）")
        tracked = sum(1 for i in self._store.ingredients.values() if i["amount"] >= 0)
        unlimited = len(self._store.ingredients) - tracked
        logger.info(
            "厨房插件已加载：白名单 %d 人，食材 %d 种（计数 %d / 常备无限 %d），钱包%s",
            len(self.config.whitelist or []),
            len(self._store.ingredients),
            tracked,
            unlimited,
            f"余额 {self._store.wallet_balance:g} 元" if self.config.wallet_enabled else "已关闭",
        )

    async def on_unload(self) -> None:
        if self._store is not None:
            self._store.save()
        self._store = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            logger.info("厨房插件配置已更新")

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    def _user_allowed(self, user_id: str) -> bool:
        blacklist = {str(u).strip() for u in (self.config.blacklist or [])}
        if user_id in blacklist:
            return False
        whitelist = {str(u).strip() for u in (self.config.whitelist or [])}
        return user_id in whitelist

    def _deny_result(self) -> dict[str, Any]:
        return {
            "success": False,
            "content": (
                "厨房和买菜钱只对主人和白名单里的朋友开放，"
                "请礼貌地告诉对方厨房暂时不对外使用，可以陪他聊别的。"
            ),
        }

    def _guard(self, kwargs: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        """公共检查：插件开启、存储就绪、用户在白名单内。"""
        if not self.config.enabled:
            return "插件未启用", {"success": False, "error": "插件未启用"}
        if self._store is None:
            return "尚未初始化", {"success": False, "error": "插件尚未完成初始化"}
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not user_id:
            return "缺少用户", {"success": False, "error": "user_id 不能为空"}
        if not self._user_allowed(user_id):
            return None, self._deny_result()
        return user_id, None

    # ------------------------------------------------------------------
    # 工具：查看厨房
    # ------------------------------------------------------------------

    @Tool(
        "kitchen_query",
        description=(
            "查看你的厨房现状：现在有哪些食材、各剩多少、买菜钱余额、最近做过什么饭。"
            "适用场景：用户问\"今天吃什么\"\"厨房里还有什么\"，或者你准备做饭前想确认食材；"
            "凡是聊到吃的、想了解家里有什么原料时，先调用本工具再回答，不要凭空编造厨房里有什么。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="说话用户的 QQ 号",
                required=True,
            ),
        ],
        visibility="visible",
    )
    async def kitchen_query(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：查看厨房。"""
        _user_id, deny = self._guard(kwargs)
        if deny:
            return deny
        store = self._store
        assert store is not None

        tracked = sorted(
            (name for name, info in store.ingredients.items() if info["amount"] >= 0)
        )
        unlimited = sorted(
            (name for name, info in store.ingredients.items() if info["amount"] < 0)
        )
        lines = ["厨房现状："]
        if tracked:
            lines.append("【新鲜食材】" + "、".join(store.describe(n) for n in tracked))
        else:
            lines.append("【新鲜食材】空空如也，该去买菜了")
        if unlimited:
            lines.append("【常备无限】" + "、".join(unlimited) + "（米面调料管够）")
        if self.config.wallet_enabled:
            lines.append(f"【买菜钱】余额 {store.wallet_balance:g} 元")
        if store.meals:
            recent = store.meals[-3:]
            recent.reverse()
            lines.append(
                "【最近做的饭】"
                + "、".join(f"{m.get('dish', '?')}（{_relative_time(m.get('ts', 0))}）" for m in recent)
            )
        content = "\n".join(lines)
        return {
            "success": True,
            "content": content,
            "inventory": {
                name: (
                    "无限" if info["amount"] < 0 else f"{fmt_amount(info['amount'])}{info['unit'] or ''}"
                )
                for name, info in store.ingredients.items()
            },
            "wallet_balance": store.wallet_balance if self.config.wallet_enabled else None,
        }

    # ------------------------------------------------------------------
    # 工具：做饭
    # ------------------------------------------------------------------

    @Tool(
        "kitchen_cook",
        description=(
            "你亲自下厨做饭时调用：把用掉的食材从厨房里扣掉。做饭只消耗食材，"
            "绝不涉及花钱（买菜时已经付过钱），本工具没有金额参数，也不要因为做饭去动钱包。"
            "参数说明：dish（必填）菜名，如\"玉米萝卜猪肉馅饺子\"；"
            "used_ingredients（必填）这次用掉的新鲜食材清单，格式为\"名称 数量单位\"用逗号分隔，"
            "例如\"猪肉 1斤，玉米 2根，白萝卜 1根，饺子皮 30张\"；"
            "用量按做一顿饭的常识估计，别夸张（一顿饺子大约用 1 斤肉、30 张皮），"
            "米面调料是常备无限的不用列。"
            "如果这顿吃的东西之前已用补货工具作为成品登记过（例如\"玉米萝卜肉馅饺子（生） 60个\"），"
            "这里直接按完整名字扣那份成品即可（如\"玉米萝卜肉馅饺子（生） 15个\"），不要再扣一遍原材料。"
            "做出来的东西如果要存放（生饺子冻冰箱、一锅汤留到晚上），"
            "做完紧接着调用补货工具把成品按完整名字登记入库（如\"玉米萝卜肉馅饺子（生） 60个\"），"
            "这样之后吃的时候才有账可扣。"
            "做完后把结果自然地告诉用户，比如做了什么、用了什么、还剩什么。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="说话用户的 QQ 号",
                required=True,
            ),
            ToolParameterInfo(
                name="dish",
                param_type=ToolParamType.STRING,
                description="菜名，如\"西红柿炒鸡蛋\"",
                required=True,
            ),
            ToolParameterInfo(
                name="used_ingredients",
                param_type=ToolParamType.STRING,
                description=(
                    "用掉的食材清单，格式\"名称 数量单位\"逗号分隔，"
                    "例如\"猪肉 1斤，玉米 2根，饺子皮 30张\""
                ),
                required=True,
            ),
        ],
        visibility="visible",
    )
    async def kitchen_cook(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：做饭并扣减食材。"""
        user_id, deny = self._guard(kwargs)
        if deny:
            return deny
        store = self._store
        assert store is not None

        dish = str(kwargs.get("dish", "") or "").strip()
        spec = str(kwargs.get("used_ingredients", "") or "").strip()
        if not dish:
            return {"success": False, "error": "dish 不能为空"}
        if not spec:
            return {"success": False, "error": "used_ingredients 不能为空"}

        items, bad = parse_items(spec)
        if not items:
            return {
                "success": False,
                "error": f"食材清单没法解析：{spec}，请按\"名称 数量单位\"的格式重试",
            }

        used_desc: list[str] = []
        notes: list[str] = []
        for name, amount, unit in items:
            key = store.find(name)
            if key is None:
                notes.append(f"厨房里没有{name}，这次没放")
                continue
            info = store.ingredients[key]
            if info["amount"] < 0:
                used_desc.append(f"{key}（常备）")
                continue
            if info["amount"] + 1e-9 < amount:
                notes.append(
                    f"{key}只剩{fmt_amount(info['amount'])}{info['unit'] or ''}，"
                    f"不够{fmt_amount(amount)}{unit or ''}，已经全部用上了"
                )
                info["amount"] = 0.0
                used_desc.append(f"{key}（全部用上，已用完）")
                continue
            info["amount"] = round(info["amount"] - amount, 3)
            used_desc.append(f"{key} {fmt_amount(amount)}{unit or info['unit'] or ''}"
                             + (f"（还剩{fmt_amount(info['amount'])}{info['unit'] or ''}）" if info["amount"] > 0 else "（用完了）"))

        meal: dict[str, Any] = {
            "ts": time.time(),
            "dish": dish,
            "used": [f"{n} {fmt_amount(a)}{u}" if a >= 0 else f"{n}（常备）" for n, a, u in items],
            "by": user_id,
        }
        store.add_meal(meal, int(self.config.max_meals_log))
        store.save()

        parts = [f"做好了\"{dish}\"。用掉：{'、'.join(used_desc) if used_desc else '（没用新鲜食材）'}。"]
        if notes:
            parts.append("注意：" + "；".join(notes) + "。")
            parts.append("缺的食材可以提醒用户补货，或者你自己去菜市场买（用厨房补货工具）。")
        return {
            "success": True,
            "content": "".join(parts),
            "dish": dish,
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # 工具：补货 / 清理
    # ------------------------------------------------------------------

    @Tool(
        "kitchen_stock",
        description=(
            "厨房进货：新增或补充食材、把某种食材设为常备无限、清理放坏的东西。"
            "适用场景：用户说\"我买了 5 斤肉放冰箱\"\"家里没鸡蛋了帮我加了 10 个\"，"
            "或者你刚才自己去买菜回来把东西放进厨房。"
            "你亲手做出来的食物（包好的生饺子、炖好的一锅汤、腌好的咸菜等）也用它登记入库，"
            "例如 changes 写\"玉米萝卜肉馅饺子 40个\"；"
            "凡是你要告诉用户\"做好了/包好了/炖好了\"的食物，必须先调用本工具登记，"
            "没登记进厨房的东西就等于不存在，绝不能凭空声称家里有。"
            "参数说明：changes（必填）要补充的食材或成品，格式\"名称 数量单位\"逗号分隔，"
            "例如\"猪肉 2斤，鸡蛋 6个\"；新食材写上单位即可；要把某样设为常备无限就写\"名称 无限\"。"
            "cost（可选）这次进货花的钱，会从买菜钱里扣（钱包关闭时忽略）；"
            "你角色扮演去菜市场买菜时，请估个合理价格填上，比如猪肉 2 斤大约 30 元。"
            "remove_items（可选）要整个从厨房撤掉的食材名（放坏了、过期了），逗号分隔。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="说话用户的 QQ 号",
                required=True,
            ),
            ToolParameterInfo(
                name="changes",
                param_type=ToolParamType.STRING,
                description='要补充的食材或成品，格式"名称 数量单位"逗号分隔，如"猪肉 2斤，鸡蛋 6个"',
                required=True,
            ),
            ToolParameterInfo(
                name="cost",
                param_type=ToolParamType.NUMBER,
                description="这次进货花的钱（元），会从买菜钱里扣，可省略",
                required=False,
            ),
            ToolParameterInfo(
                name="remove_items",
                param_type=ToolParamType.STRING,
                description="要整个撤掉的食材名，逗号分隔，可省略",
                required=False,
            ),
            ToolParameterInfo(
                name="note",
                param_type=ToolParamType.STRING,
                description="备注（谁买的、放了哪等），可省略",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def kitchen_stock(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：补充或清理食材。"""
        _user_id, deny = self._guard(kwargs)
        if deny:
            return deny
        store = self._store
        assert store is not None

        spec = str(kwargs.get("changes", "") or "").strip()
        remove_spec = str(kwargs.get("remove_items", "") or "").strip()
        note = str(kwargs.get("note", "") or "").strip()
        if not spec and not remove_spec:
            return {"success": False, "error": "changes 和 remove_items 至少要填一个"}

        changes_desc: list[str] = []
        bad: list[str] = []
        if spec:
            items, bad = parse_items(spec)
            if not items and not bad:
                return {"success": False, "error": f"清单没法解析：{spec}"}
            for name, amount, unit in items:
                key = store.find(name) or name
                info = store.ingredients.get(key)
                if amount < 0:
                    store.ingredients[key] = {"amount": UNLIMITED, "unit": unit or "份"}
                    changes_desc.append(f"{key}设为常备无限")
                elif info is None:
                    store.ingredients[key] = {"amount": amount, "unit": unit or "份"}
                    changes_desc.append(f"新添了{key} {fmt_amount(amount)}{unit or ''}")
                elif info["amount"] < 0:
                    changes_desc.append(f"{key}本来就是常备无限，保持不变")
                else:
                    info["amount"] = round(info["amount"] + amount, 3)
                    if unit:
                        info["unit"] = unit
                    changes_desc.append(f"{key}补充{fmt_amount(amount)}{unit or ''}，现在{store.describe(key)}")

        removed_desc: list[str] = []
        if remove_spec:
            for name in re.split(r"[，,、;；\n]+", remove_spec):
                name = name.strip()
                if not name:
                    continue
                key = store.find(name)
                if key is None:
                    removed_desc.append(f"没找到{name}")
                else:
                    del store.ingredients[key]
                    removed_desc.append(f"撤掉了{key}")

        wallet_note = ""
        cost_raw = kwargs.get("cost")
        if cost_raw not in (None, ""):
            try:
                cost = float(cost_raw)
            except (TypeError, ValueError):
                cost = 0.0
            if cost > 0:
                if self.config.wallet_enabled:
                    store.wallet_balance = round(store.wallet_balance - cost, 2)
                    summary = spec if len(spec) <= 40 else spec[:40] + "…"
                    store.add_transaction(-cost, f"买菜：{summary}")
                    store.transactions = store.transactions[-max(int(self.config.max_transactions), 10):]
                    wallet_note = f"，买菜钱扣了 {cost:g} 元，余额 {store.wallet_balance:g} 元"
                else:
                    wallet_note = "（钱包已关闭，这次没记账）"

        store.save()
        parts = ["厨房更新完成："]
        if changes_desc:
            parts.append("；".join(changes_desc) + "。")
        if removed_desc:
            parts.append("；".join(removed_desc) + "。")
        parts.append(wallet_note.rstrip("，") + ("。" if wallet_note else ""))
        if bad:
            parts.append(f"有几项没看懂已跳过：{'、'.join(bad)}。")
        if note:
            parts.append(f"备注：{note}。")
        return {
            "success": True,
            "content": "".join(p for p in parts if p),
            "changed": changes_desc,
            "removed": removed_desc,
        }

    # ------------------------------------------------------------------
    # 工具：买菜钱包
    # ------------------------------------------------------------------

    @Tool(
        "kitchen_wallet",
        description=(
            "管理买菜钱（虚拟钱包，记账用）。action 三选一："
            "\"余额\"查现在剩多少钱和最近流水；"
            "\"充值\"主人给你零花钱时用（amount 填金额）；"
            "\"支出\"你单独花钱时用（比如嘴馋买了个小蛋糕，amount 填金额）。"
            "注意：正常买菜的花费在补货工具里填 cost 就行，不要在这里再记一遍，避免重复扣钱。"
            "钱包在插件设置里关闭时本工具不可用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="说话用户的 QQ 号",
                required=True,
            ),
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description='操作："余额"、"充值"或"支出"',
                required=True,
            ),
            ToolParameterInfo(
                name="amount",
                param_type=ToolParamType.NUMBER,
                description="充值或支出的金额（正数），查余额时不用填",
                required=False,
            ),
            ToolParameterInfo(
                name="desc",
                param_type=ToolParamType.STRING,
                description="说明（比如\"主人给的零花钱\"\"买了个小蛋糕\"），可省略",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def kitchen_wallet(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：虚拟买菜钱包。"""
        _user_id, deny = self._guard(kwargs)
        if deny:
            return deny
        if not self.config.wallet_enabled:
            return {"success": False, "content": "买菜钱包当前是关闭状态，不用记账。"}
        store = self._store
        assert store is not None

        action = str(kwargs.get("action", "") or "").strip()
        desc = str(kwargs.get("desc", "") or "").strip()
        amount_raw = kwargs.get("amount")
        try:
            amount = float(amount_raw) if amount_raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            amount = 0.0

        limit = max(int(self.config.max_transactions), 10)
        if action.startswith("充"):
            if amount <= 0:
                return {"success": False, "error": "充值请填写正确的金额（amount）"}
            store.wallet_balance = round(store.wallet_balance + amount, 2)
            store.add_transaction(amount, desc or "充值")
            store.transactions = store.transactions[-limit:]
            store.save()
            return {
                "success": True,
                "content": f"收到 {amount:g} 元，买菜钱现在有 {store.wallet_balance:g} 元。",
                "balance": store.wallet_balance,
            }
        if action.startswith(("支", "花", "买")):
            if amount <= 0:
                return {"success": False, "error": "支出请填写正确的金额（amount）"}
            store.wallet_balance = round(store.wallet_balance - amount, 2)
            store.add_transaction(-amount, desc or "支出")
            store.transactions = store.transactions[-limit:]
            store.save()
            content = f"花掉 {amount:g} 元，买菜钱还剩 {store.wallet_balance:g} 元。"
            if store.wallet_balance < 0:
                content += "余额已经不够了，记得向主人撒娇要零花钱（充值）。"
            return {"success": True, "content": content, "balance": store.wallet_balance}

        recent = list(reversed(store.transactions[-10:]))
        lines = [f"买菜钱余额：{store.wallet_balance:g} 元。"]
        if recent:
            lines.append("最近流水：" + "；".join(
                f"{_relative_time(t.get('ts', 0))}{t.get('desc', '')}{t.get('amount', 0):+g}元"
                for t in recent
            ))
        else:
            lines.append("还没有花过钱。")
        return {"success": True, "content": "\n".join(lines), "balance": store.wallet_balance}


    # ------------------------------------------------------------------
    # WebUI 管理 API（网页后台的"麦麦的厨房"页面用）
    # ------------------------------------------------------------------

    def _require_store(self) -> KitchenStore:
        if self._store is None:
            raise RuntimeError("插件尚未完成初始化，请稍后重试")
        return self._store

    @API("kitchen_state", version="1")
    async def api_kitchen_state(self) -> dict[str, Any]:
        """WebUI 查询：当前库存（含分类）、钱包和最近做饭记录。"""
        store = self._require_store()
        items = [
            {
                "name": name,
                "amount": info["amount"],
                "unit": info["unit"],
                "cat": info.get("cat", CAT_OTHER),
            }
            for name, info in sorted(store.ingredients.items())
        ]
        return {
            "categories": CATEGORIES,
            "items": items,
            "wallet": {
                "enabled": bool(self.config.wallet_enabled),
                "balance": store.wallet_balance,
            },
            "meals": [
                {"dish": m.get("dish", "?"), "ts": m.get("ts", 0)}
                for m in reversed(store.meals[-10:])
            ],
        }

    @API("kitchen_set_item", version="1")
    async def api_kitchen_set_item(
        self, name: str, amount: float, unit: str = "", cat: str = ""
    ) -> dict[str, Any]:
        """WebUI 操作：新增或覆盖一个食材。amount 传 -1 表示设为无限常备。"""
        store = self._require_store()
        name = str(name or "").strip()
        if not name:
            raise ValueError("食材名称不能为空")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise ValueError("数量必须是数字") from None
        if amount < 0 and amount != UNLIMITED:
            raise ValueError("数量不能是负数（要设为无限常备请填 -1）")
        unit = str(unit or "").strip() or "份"
        cat = str(cat or "").strip()
        if cat not in CATEGORIES:
            cat = _DEFAULT_CATEGORY.get(name, CAT_OTHER)
        store.ingredients[name] = {"amount": amount, "unit": unit, "cat": cat}
        store.save()
        display = "无限常备" if amount < 0 else f"{fmt_amount(amount)}{unit}"
        return {"name": name, "display": display, "cat": cat}

    @API("kitchen_remove_item", version="1")
    async def api_kitchen_remove_item(self, name: str) -> dict[str, Any]:
        """WebUI 操作：从厨房整个撤掉一个食材。"""
        store = self._require_store()
        key = store.find(str(name or "").strip())
        if key is None:
            raise ValueError(f"厨房里没有「{name}」")
        del store.ingredients[key]
        store.save()
        return {"removed": key}

    @API("kitchen_set_wallet", version="1")
    async def api_kitchen_set_wallet(self, balance: float) -> dict[str, Any]:
        """WebUI 操作：直接修正买菜钱余额（差额会记一笔流水便于对账）。"""
        store = self._require_store()
        try:
            balance = float(balance)
        except (TypeError, ValueError):
            raise ValueError("余额必须是数字") from None
        if balance < 0:
            raise ValueError("余额不能是负数")
        old = store.wallet_balance
        store.wallet_balance = round(balance, 2)
        store.add_transaction(round(store.wallet_balance - old, 2), "网页手动修正余额")
        store.transactions = store.transactions[
            -max(int(self.config.max_transactions), 10):
        ]
        store.save()
        return {"balance": store.wallet_balance}


def create_plugin() -> MaiKitchenPlugin:
    return MaiKitchenPlugin()
