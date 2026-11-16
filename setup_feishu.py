# -*- coding: utf-8 -*-
"""
交互式配置飞书群机器人推送。
运行：双击「配置飞书推送.bat」，或 python setup_feishu.py

做三件事：
  1. 把你的飞书 webhook 写进本地 config.json（这个文件不会提交到仓库）
  2. 立刻发一条测试消息，确认通不通
  3. 打印一段 JSON，让你贴到 GitHub 的 Secret 里，线上才能推
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "config.json"
SITE = "https://chouyangt.github.io/policy-radar/"


def load_cfg() -> dict:
    if CFG.exists():
        try:
            return json.loads(CFG.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def save_cfg(cfg: dict):
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    print("=" * 58)
    print("  政策雷达 · 配置飞书推送")
    print("=" * 58)
    print()
    print("先在飞书里拿到机器人地址（1 分钟）：")
    print("  1. 打开飞书，进入你想接收政策的群（没有就新建一个，只有自己也行）")
    print("  2. 群右上角 「···」→ 设置 → 群机器人 → 添加机器人")
    print("  3. 选「自定义机器人」，名字随便写，比如「政策雷达」")
    print("  4. 复制它给出的 Webhook 地址")
    print()
    print("  注意：如果勾选了「签名校验」，等会儿还要填一个 secret")
    print()

    url = input("① 粘贴 Webhook 地址：").strip()
    if not url.startswith("http"):
        print("\n× 地址不对，应该以 https:// 开头。请重新运行。")
        return 1
    if "feishu" not in url and "larksuite" not in url:
        print("\n! 警告：这个地址看起来不像飞书 webhook，仍然继续试试。")

    secret = input("② 签名校验 secret（没开就直接回车）：").strip()

    try:
        min_imp = input("③ 什么级别的政策才推？1-5，回车默认 4（★4 以上）：").strip() or "4"
        min_imp = max(1, min(5, int(min_imp)))
    except ValueError:
        min_imp = 4

    cfg = load_cfg()
    n = cfg.setdefault("notify", {})
    n["feishu"] = url
    if secret:
        n["feishu_secret"] = secret
    elif "feishu_secret" in n:
        del n["feishu_secret"]
    n["site_url"] = SITE
    n["min_imp"] = min_imp
    n.setdefault("max_items", 10)
    save_cfg(cfg)
    print(f"\n✓ 已写入 {CFG.name}（这个文件不会被提交到仓库）")

    print("\n正在发送测试消息…")
    sys.path.insert(0, str(ROOT))
    import notify

    demo = [
        {"title": "✅ 政策雷达已接通飞书", "grp": "深圳", "src": "配置测试",
         "date": "2026-11-16", "imp": 5, "deadline": "2026-11-20",
         "action": "以后有新的重点政策，会自动推到这个群", "url": SITE},
        {"title": "示例：深圳市人社局 · 引才伯乐奖申报", "grp": "深圳", "src": "深圳市人社局",
         "date": "2026-11-16", "imp": 4, "deadline": "2026-11-27",
         "action": "核对申报条件，倒排材料清单，卡住截止时间提交",
         "url": "https://hrss.sz.gov.cn/"},
    ]
    ok = notify.send(demo, SITE)
    if not ok:
        print("\n× 没发出去。检查 webhook 地址是否完整、机器人是否被移出群。")
        return 1

    print()
    print("=" * 58)
    print("  本地已经通了。现在还差一步：让 GitHub 上的定时任务也能推。")
    print("=" * 58)
    print()
    print("打开这个页面：")
    print("  https://github.com/Chouyangt/policy-radar/settings/secrets/actions/new")
    print()
    print("  Name  填： CONFIG_JSON")
    print("  Secret 填：下面这一整段（含大括号）")
    print()
    payload = json.dumps({"notify": n}, ensure_ascii=False)
    print("-" * 58)
    print(payload)
    print("-" * 58)
    print()
    print("填完点 Add secret。之后每小时 3 轮，有 ★%d 以上的新政策就自动推到飞书。" % min_imp)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
