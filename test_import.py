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
