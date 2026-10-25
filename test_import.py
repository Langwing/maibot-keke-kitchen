import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plugin  # noqa: E402

p = plugin.create_plugin()
tools = [n for n in dir(p) if n.startswith("kitchen_") and not n.startswith("api_")]
apis = [n for n in dir(p) if n.startswith("api_")]
print("PLUGIN_OK tools:", tools, "apis:", apis)
assert len(tools) == 4, f"expect 4 tools, got {tools}"
assert sorted(apis) == [
    "api_kitchen_remove_item",
    "api_kitchen_set_item",
    "api_kitchen_set_wallet",
    "api_kitchen_state",
], apis

# 初始清单与解析函数校验
assert len(plugin.DEFAULT_INGREDIENTS) == 38
unlimited = [v for v in plugin.DEFAULT_INGREDIENTS.values() if v[0] == plugin.UNLIMITED]
assert len(unlimited) == 21, f"expect 21 unlimited, got {len(unlimited)}"

items, bad = plugin.parse_items("猪肉 1斤，2斤猪肉，米 无限")
assert not bad, bad
assert items[0] == ("猪肉", 1.0, "斤"), items[0]
assert items[1] == ("猪肉", 2.0, "斤"), items[1]  # “数量在前”的写法
assert items[2] == ("米", plugin.UNLIMITED, "无限"), items[2]
print("PARSE_OK items:", items)

# find() 模糊匹配回归：长名字成品不能吸并到短原料（玉米饺子→玉米 的 bug）
store = plugin.KitchenStore(Path("t") , 0.0)
store.ingredients = {
    "猪肉": {"amount": 3.0, "unit": "斤", "cat": "肉类"},
    "玉米": {"amount": 1.0, "unit": "根", "cat": "蔬菜"},
    "饺子皮": {"amount": 60.0, "unit": "张", "cat": "主食粮油"},
    "玉米萝卜肉馅饺子（生）": {"amount": 60.0, "unit": "个", "cat": "长期食品"},
}
assert store.find("猪肉") == "猪肉"
assert store.find("肉") == "猪肉"            # 简短叫法
assert store.find("新鲜猪肉") == "猪肉"       # 带修饰词
assert store.find("玉米") == "玉米"           # 精确匹配优先，不落到饺子成品上
assert store.find("玉米萝卜肉馅饺子（生）") == "玉米萝卜肉馅饺子（生）"
assert store.find("玉米萝卜肉馅饺子") == "玉米萝卜肉馅饺子（生）"  # 差个括号后缀能对上
assert store.find("韭菜鸡蛋馅饺子（生）") is None  # 新成品名：不该吸并到任何原料
print("FIND_OK")

# prune() 容量兜底：超上限时只清用完的条目，有效库存不动
store2 = plugin.KitchenStore(Path("t2"), 0.0)
for i in range(10):
    store2.ingredients[f"有效食材{i}"] = {"amount": float(i + 1), "unit": "份", "cat": "其他"}
for i in range(5):
    store2.ingredients[f"用完的{i}"] = {"amount": 0.0, "unit": "份", "cat": "其他"}
removed = store2.prune(12)
assert len(store2.ingredients) == 12, len(store2.ingredients)
assert all(r.startswith("用完的") for r in removed) and len(removed) == 3, removed
removed2 = store2.prune(5)  # 到不了上限：清光用完的即止，有效库存绝不自动删
assert len(store2.ingredients) == 10 and all(r.startswith("用完的") for r in removed2), removed2
print("PRUNE_OK removed:", removed, removed2)
