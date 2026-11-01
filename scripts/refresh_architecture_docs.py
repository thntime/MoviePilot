"""按最新架构基线刷新 docs/architecture-overview.md 中被测试断言的数字。

合并上游后代码变了，架构基线需要重新生成；文档里被 `test_architecture_contract_baseline`
断言的几处数字也必须跟着变，否则同步完成后测试会红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = ROOT / "tests" / "fixtures" / "architecture"
OVERVIEW = ROOT / "docs" / "architecture-overview.md"


def _load(name: str) -> dict:
    return json.loads((BASELINE_ROOT / name).read_text(encoding="utf-8"))


def main() -> int:
    """刷新文档中由基线派生的数字，返回进程退出码。"""
    dependency = _load("dependency-baseline.json")
    runtime = _load("runtime-contract-baseline.json")
    facts = runtime["event_facts"]
    text = OVERVIEW.read_text(encoding="utf-8")
    original = text

    text = re.sub(
        r"(\| 内部导入边 \| )[\d,]+( \|)",
        lambda match: f"{match.group(1)}{dependency['edge_count']:,}{match.group(2)}",
        text,
    )
    text = re.sub(
        r"当前宿主有 \d+ 个\n生产调用，其中 \d+ 个静态解析为 \d+ 个事件引用",
        (
            f"当前宿主有 {facts['producer_call_count']} 个\n"
            f"生产调用，其中 {facts['static_producer_call_count']} 个静态解析为 "
            f"{facts['producer_event_reference_count']} 个事件引用"
        ),
        text,
    )
    text = re.sub(
        r"\d+ 个消费注册中 \d+ 个静态、\d+ 个动态",
        (
            f"{facts['consumer_registration_count']} 个消费注册中 "
            f"{facts['static_consumer_count']} 个静态、{facts['dynamic_consumer_count']} 个动态"
        ),
        text,
    )

    if text == original:
        print("架构文档无需刷新")
        return 0
    OVERVIEW.write_text(text, encoding="utf-8")
    print("已刷新 docs/architecture-overview.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
