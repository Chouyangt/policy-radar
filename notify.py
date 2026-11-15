# -*- coding: utf-8 -*-
"""
新政策推送。配置写在 config.json 的 notify 段，支持：
  企业微信群机器人 / 钉钉群机器人 / 飞书群机器人 / Telegram / Bark(iOS)
一次可以配多个，配了哪些就发哪些。没配就静默跳过。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _post(url: str, payload: dict, timeout: int = 20) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    p = subprocess.run(
        ["curl.exe" if os.name == "nt" else "curl", "-sS", "-X", "POST",
         "-H", "Content-Type: application/json", "--max-time", str(timeout),
         "--data-binary", "@-", url],
        input=body, capture_output=True, timeout=timeout + 10,
    )
    return (p.stdout or b"").decode("utf-8", "ignore")[:200]


def _get(url: str, timeout: int = 20) -> str:
    p = subprocess.run(["curl.exe" if os.name == "nt" else "curl", "-sS",
                        "--max-time", str(timeout), url],
                       capture_output=True, timeout=timeout + 10)
    return (p.stdout or b"").decode("utf-8", "ignore")[:200]


def build_text(items: list[dict], site_url: str = "") -> str:
    """拼一条适合手机看的短消息。"""
    lines = [f"【政策雷达】新增 {len(items)} 条重点政策"]
    for it in items[:10]:
        star = "★" * int(it.get("imp") or 3)
        lines.append(f"\n{star} {it['title'][:52]}")
        lines.append(f"  {it.get('grp', '')} · {it.get('src', '')} · {it.get('date', '')}")
        if it.get("deadline"):
            lines.append(f"  截止 {it['deadline']}")
        if it.get("action"):
            lines.append(f"  → {it['action'][:48]}")
        if it.get("url"):
            lines.append(f"  {it['url']}")
    if site_url:
        lines.append(f"\n日历：{site_url}")
    return "\n".join(lines)


def send(items: list[dict], site_url: str = "") -> bool:
    try:
        from radar import user_config
        cfg = user_config()
    except Exception:
        cfg = {}
        p = ROOT / "config.json"
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception:
                cfg = {}
    n = cfg.get("notify") or {}
    if not n or not items:
        return False

    text = build_text(items, site_url or n.get("site_url", ""))
    sent = False

    def try_send(name, fn):
        nonlocal sent
        try:
            out = fn()
            print(f"    · 推送 {name}：{out or 'ok'}")
            sent = True
        except Exception as e:
            print(f"    ! 推送 {name} 失败：{e}")

    if n.get("wecom"):
        try_send("企业微信", lambda: _post(n["wecom"], {"msgtype": "text", "text": {"content": text}}))
    if n.get("dingtalk"):
        try_send("钉钉", lambda: _post(n["dingtalk"], {"msgtype": "text", "text": {"content": text}}))
    if n.get("feishu"):
        try_send("飞书", lambda: _post(n["feishu"], {"msg_type": "text", "content": {"text": text}}))
    if n.get("telegram_token") and n.get("telegram_chat"):
        try_send("Telegram", lambda: _post(
            f"https://api.telegram.org/bot{n['telegram_token']}/sendMessage",
            {"chat_id": n["telegram_chat"], "text": text, "disable_web_page_preview": True}))
    if n.get("bark"):
        base = n["bark"].rstrip("/")
        try_send("Bark", lambda: _get(f"{base}/{text[:300].replace(chr(10), ' ')}"))
    return sent


if __name__ == "__main__":
    # 手动测试：python notify.py
    demo = [{"title": "测试：政策雷达推送通道", "grp": "深圳", "src": "测试",
             "date": "2026-11-15", "imp": 5, "action": "收到说明配置正确",
             "url": "https://www.sz.gov.cn/", "deadline": ""}]
    print("已发送" if send(demo) else "未配置 notify，什么都没发")
