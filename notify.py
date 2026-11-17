# -*- coding: utf-8 -*-
"""
新政策推送。配置写在 config.json 的 notify 段，支持：
  飞书群机器人（卡片消息） / 企业微信 / 钉钉 / Telegram / Bark(iOS)
配了哪些就发哪些，没配就静默跳过。

飞书一段的样子：
  "notify": {
      "feishu": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
      "feishu_secret": "如果开了签名校验才填",
      "site_url": "https://chouyangt.github.io/policy-radar/",
      "min_imp": 4,
      "max_items": 10
  }
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CURL = "curl.exe" if os.name == "nt" else "curl"


# ══════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════

def _post(url: str, payload: dict, timeout: int = 20) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    p = subprocess.run(
        [CURL, "-sS", "-X", "POST",
         "-H", "Content-Type: application/json; charset=utf-8",
         "--max-time", str(timeout), "--data-binary", "@-", url],
        input=body, capture_output=True, timeout=timeout + 10,
    )
    return (p.stdout or b"").decode("utf-8", "ignore")


def _get(url: str, timeout: int = 20) -> str:
    p = subprocess.run([CURL, "-sS", "--max-time", str(timeout), url],
                       capture_output=True, timeout=timeout + 10)
    return (p.stdout or b"").decode("utf-8", "ignore")


def _ok(channel: str, raw: str) -> str:
    """判断各平台返回是不是真的成功，别把失败当成功。"""
    txt = (raw or "").strip()
    try:
        d = json.loads(txt)
    except Exception:
        return f"⚠ {channel} 返回无法解析：{txt[:120]}"

    # 飞书
    if "code" in d:
        return "ok" if d.get("code") == 0 else f"⚠ 飞书错误 {d.get('code')}: {d.get('msg')}"
    if "StatusCode" in d:
        return "ok" if d.get("StatusCode") == 0 else f"⚠ 飞书错误 {d.get('StatusCode')}: {d.get('StatusMessage')}"
    # 企业微信 / 钉钉
    if "errcode" in d:
        return "ok" if d.get("errcode") == 0 else f"⚠ {channel} 错误 {d.get('errcode')}: {d.get('errmsg')}"
    # Telegram
    if "ok" in d:
        return "ok" if d["ok"] else f"⚠ Telegram 错误：{txt[:120]}"
    return "ok" if not d else f"⚠ 未知返回：{txt[:120]}"


# ══════════════════════════════════════════════════════════════
# 消息体
# ══════════════════════════════════════════════════════════════

def _star(imp) -> str:
    try:
        n = max(1, min(5, int(imp)))
    except (TypeError, ValueError):
        n = 3
    return "★" * n


def build_text(items: list[dict], site_url: str = "") -> str:
    """纯文本版：企业微信 / 钉钉 / Telegram / Bark 用。"""
    lines = [f"【政策雷达】新增 {len(items)} 条重点"]
    for it in items:
        lines.append("")
        lines.append(f"{_star(it.get('imp'))} {it['title']}")
        meta = f"{it.get('grp', '')} · {it.get('src', '')} · {it.get('date', '')}"
        lines.append(meta)
        if it.get("deadline"):
            lines.append(f"⏰ 截止 {it['deadline']}")
        if it.get("action"):
            lines.append(f"💡 {it['action']}")
        if it.get("url"):
            lines.append(it["url"])
    if site_url:
        lines.append("")
        lines.append(f"日历：{site_url}")
    return "\n".join(lines)


def build_feishu_card(items: list[dict], site_url: str = "") -> dict:
    """飞书卡片版：每条一个区块，链接可点，比纯文本好读很多。"""
    top = max((int(it.get("imp") or 3) for it in items), default=3)
    color = "red" if top >= 5 else "orange" if top >= 4 else "blue"

    els = []
    for i, it in enumerate(items):
        title = it["title"]
        if len(title) > 62:
            title = title[:61] + "…"
        lines = [
            f"**{title}**",
            f"{_star(it.get('imp'))}  {it.get('grp', '')} · {it.get('src', '')} · {it.get('date', '')}",
        ]
        if it.get("deadline"):
            lines.append(f"<font color='red'>⏰ 截止 {it['deadline']}</font>")
        if it.get("action"):
            lines.append(f"💡 {it['action']}")
        if it.get("url"):
            lines.append(f"[查看原文]({it['url']})")

        els.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        if i < len(items) - 1:
            els.append({"tag": "hr"})

    if site_url:
        els.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开日历"},
                "type": "primary",
                "url": site_url,
            }],
        })

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"政策雷达 · 新增 {len(items)} 条"},
            "template": color,
        },
        "elements": els,
    }


def feishu_sign(secret: str) -> tuple[str, str]:
    """飞书自定义机器人开了「签名校验」时用。"""
    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return ts, base64.b64encode(h).decode("utf-8")


# ══════════════════════════════════════════════════════════════
# 发送
# ══════════════════════════════════════════════════════════════

def load_cfg() -> dict:
    try:
        from radar import user_config
        return user_config()
    except Exception:
        p = ROOT / "config.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception:
                return {}
    return {}


def send(items: list[dict], site_url: str = "") -> str:
    """返回一句人话状态，例如「飞书：ok」/「未配置推送」，会写进日历页脚。"""
    cfg = load_cfg()
    n = cfg.get("notify") or {}
    if not items:
        return "本轮无可推送内容"
    if not n:
        return "未配置推送通道"

    max_items = int(n.get("max_items") or 10)
    items = sorted(items, key=lambda x: -(int(x.get("imp") or 3)))[:max_items]
    site = site_url or n.get("site_url", "")
    text = build_text(items, site)
    results: list[str] = []

    def try_send(name, fn):
        try:
            raw = fn()
            status = _ok(name, raw)
            results.append(f"{name}：{status}")
            print(f"    · {name}：{status}")
        except Exception as e:
            results.append(f"{name}：异常 {e}")
            print(f"    ! {name} 失败：{e}")

    if n.get("feishu"):
        payload = {"msg_type": "interactive", "card": build_feishu_card(items, site)}
        if n.get("feishu_secret"):
            ts, sign = feishu_sign(n["feishu_secret"])
            payload.update({"timestamp": ts, "sign": sign})
        try_send("飞书", lambda: _post(n["feishu"], payload))

    if n.get("wecom"):
        try_send("企业微信", lambda: _post(n["wecom"], {"msgtype": "text", "text": {"content": text}}))

    if n.get("dingtalk"):
        try_send("钉钉", lambda: _post(n["dingtalk"], {"msgtype": "text", "text": {"content": text}}))

    if n.get("telegram_token") and n.get("telegram_chat"):
        try_send("Telegram", lambda: _post(
            f"https://api.telegram.org/bot{n['telegram_token']}/sendMessage",
            {"chat_id": n["telegram_chat"], "text": text, "disable_web_page_preview": True}))

    if n.get("bark"):
        base = n["bark"].rstrip("/")
        try_send("Bark", lambda: _get(f"{base}/{text[:300].replace(chr(10), ' ')}"))

    return "；".join(results) if results else "无可用通道"


# ══════════════════════════════════════════════════════════════
# 命令行
# ══════════════════════════════════════════════════════════════

def main():
    cfg = load_cfg()
    n = cfg.get("notify") or {}
    if not n:
        print("config.json 里没有 notify 段，什么都没发。")
        print("先双击「配置飞书推送.bat」，或在 config.json 里填 notify。")
        return

    demo = [
        {"title": "测试：政策雷达推送通道", "grp": "深圳", "src": "本地测试",
         "date": "2026-11-16", "imp": 5, "deadline": "2026-11-20",
         "action": "收到这条说明配置正确，以后有新的重点政策会自动推这里",
         "url": "https://chouyangt.github.io/policy-radar/", "summary": ""},
        {"title": "测试：第二条，验证卡片排版", "grp": "国家", "src": "本地测试",
         "date": "2026-11-16", "imp": 4, "deadline": "",
         "action": "卡片里每条政策都带来源、星级和原文链接",
         "url": "https://www.gov.cn/", "summary": ""},
    ]
    print("发送测试消息…")
    status = send(demo, n.get("site_url", ""))
    print("状态：" + status)


if __name__ == "__main__":
    main()
