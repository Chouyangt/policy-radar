#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政策雷达 (policy-radar)
=======================
抓取 国家 / 广东省 / 深圳 官方政策与通知，落 SQLite，做 AI 分析，
输出「双击即可看」的自包含日历页 + 可导入手机日历的 ICS。

用法:
    python radar.py sync          # 抓取 + 分析 + 生成日历（一条龙）
    python radar.py sync --no-ai  # 只跑规则分析，不调大模型
    python radar.py render        # 只重新渲染日历
    python radar.py show          # 命令行看今天的条目
"""

from __future__ import annotations

import argparse
import hashlib
import html as ht
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
DB_PATH = DATA_DIR / "policies.db"
CONF_PATH = ROOT / "sources.json"
TEMPLATE = ROOT / "template.html"

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def now_cst() -> datetime:
    return datetime.now(CST)


def today_str() -> str:
    return now_cst().strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════
# 1. 抓取层
# ══════════════════════════════════════════════════════════════

def http_get(url: str, referer: str | None = None, timeout: int = 30) -> str:
    """
    用 curl.exe 走 Windows Schannel 取页面。
    国产政务站常见 SM2/ECC 证书，Python 自带 OpenSSL 会抛 BAD_ECPOINT，
    Schannel 能正常握手，所以这里统一走 curl。
    """
    cmd = ["curl.exe", "-sL", "--compressed", "--max-time", str(timeout),
           "-A", UA, "-H", "Accept-Language: zh-CN,zh;q=0.9"]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    raw = proc.stdout or b""
    if not raw:
        raise RuntimeError(f"空响应 (curl {proc.returncode}) {url}")
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = ht.unescape(s)
    s = (s.replace("&nbsp;", " ").replace("\u3000", " ").replace("\xa0", " ")
          .replace("\u2002", " ").replace("\u2003", " "))
    return re.sub(r"\s+", " ", s).strip()


DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")


def norm_date(y, m, d) -> str | None:
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def find_date(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    return norm_date(*m.groups()) if m else None


def date_from_url(url: str) -> str | None:
    """/n1/2026/1115/c1024-xxx.html  ->  2026-11-15"""
    m = re.search(r"/(20\d{2})/(\d{2})(\d{2})/", url)
    return norm_date(m.group(1), m.group(2), m.group(3)) if m else None


# ── 各站适配器 ────────────────────────────────────────────────

def adapt_govcn_json(src, conf):
    """中国政府网「最新政策」：静态 JSON 数组。"""
    rows = json.loads(http_get(src["url"]).strip().lstrip("\ufeff"))
    out = []
    for it in rows:
        title = clean_text(it.get("TITLE") or "")
        url = (it.get("URL") or "").strip()
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "published": (it.get("DOCRELPUBTIME") or "")[:10] or None,
            "summary": clean_text(it.get("SUB_TITLE") or ""),
        })
    return out


def adapt_gd_search(src, conf):
    """广东省统一政务搜索：按全文分类(EXT_fglb)取政策法规库。"""
    from urllib.parse import urlencode
    params = {
        "category_id": src.get("category_id", "2280"),
        "json_ext_in": json.dumps({"EXT_fglb": [src["fglb"]]},
                                  ensure_ascii=False, separators=(",", ":")),
        "order": "1",
        "including_link_doc": "1",
        "pagesize": "30",
        "callback": "cb",
    }
    raw = http_get(src["url"] + "?" + urlencode(params), referer=src.get("referer"))
    m = re.search(r"cb\((.*)\)\s*;?\s*$", raw, re.S)
    payload = json.loads(m.group(1) if m else raw)
    out = []
    for it in payload.get("results") or []:
        title = clean_text(it.get("title") or "")
        url = it.get("url") or it.get("post_url") or ""
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "published": (it.get("pub_time") or "")[:10] or None,
            "summary": clean_text(it.get("content") or "")[:400],
        })
    return out


# 深圳各局列表里混着大量采购公告、问答、工作动态，按标题剔除
DROP_RE = re.compile(
    r"(？|\?|是什么|有哪些|如何|怎么|多少|为什么|是否可以|能不能|能否|怎么办|"
    r"政府采购|询价|招标|投标|中标|废标|成交公告|采购公告|采购意向|采购项目|"
    r"人事任免公示$)"
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 政务页正文里混入的界面文字，用来判定「这段是不是正文」
UI_NOISE = re.compile(
    r"(页面原始配色|蓝底黄字|白底黑字|黄底黑字|网站无障碍|长者助手|验证码|"
    r"选择文件|提交问卷|字号|打印本页|扫一扫|返回顶部|主办单位|网站地图|"
    r"简体|繁体|适老版|退出|登录)"
)


def adapt_gd_site(src, conf):
    """
    深圳各局：走广东省统一政务搜索 /jsonp/site/<site_id>。
    一个接口覆盖 30+ 个局，返回里自带正文摘要和栏目分类，比爬各自官网稳。
    """
    from urllib.parse import urlencode
    params = {
        "page": "1",
        "pagesize": str(src.get("pagesize", 50)),
        "isgkml": "1",
        "order": "1",
        "including_url_doc": "1",
        "including_attach_doc": "1",
        "callback": "cb",
    }
    url = f"https://search.gd.gov.cn/jsonp/site/{src['site_id']}?" + urlencode(params)
    raw = http_get(url)
    m = re.search(r"cb\((.*)\)\s*;?\s*$", raw, re.S)
    payload = json.loads(m.group(1) if m else raw)

    keep = set(src.get("keep_classify") or [])
    drop = set(conf.get("drop_classify") or []) | set(src.get("drop_classify") or [])
    out = []
    for it in payload.get("results") or []:
        cls = (it.get("classify_main_name") or "").strip()
        if keep and cls not in keep:
            continue
        if cls in drop:
            continue
        title = clean_text(it.get("title") or "")
        link = it.get("url") or it.get("post_url") or ""
        if not title or not link or DROP_RE.search(title):
            continue
        out.append({
            "title": title[:160],
            "url": link,
            "published": (it.get("pub_time") or "")[:10] or None,
            "summary": clean_text(it.get("content") or "")[:600],
            "extra": cls,
        })
    return out


NAV_WORDS = ("首页", "上一页", "下一页", "更多", "登录", "注册", "网站地图", "无障碍",
             "长者助手", "简繁", "English", "返回", "打印", "关闭", "分享")


def adapt_html_list(src, conf):
    """通用政务列表页解析：<li> 里取链接 + 日期，或从 URL 反推日期。"""
    html = http_get(src["url"], referer=src.get("referer"))
    out, seen = [], set()

    if src.get("link_pattern"):
        pat = re.compile(r'href="(https?://[^"]*' + src["link_pattern"] + r'[^"]*)"[^>]*>([^<]{6,120})</a>')
        for url, title in pat.findall(html):
            t = clean_text(title)
            pub = date_from_url(url)
            if not t or url in seen or not pub:
                continue
            # 正文里被当成锚文本的整句（"文章强调，……"）不是标题
            if t.startswith(("文章", "报道", "记者", "编者", "他指出", "她指出")) or t.count("，") >= 2:
                continue
            seen.add(url)
            out.append({"title": t, "url": url, "published": pub, "summary": ""})
        return out

    for chunk in re.split(r"<li[\s>]", html):
        seg = chunk[:1800]
        a = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
        if not a:
            continue
        href, title = a.group(1), clean_text(a.group(2))
        if not title or len(title) < 8 or href in seen:
            continue
        if any(w in title for w in NAV_WORDS):
            continue
        if not re.search(r"(content/post_|/post_|/t\d{8}_|\.shtml|\.html|\.htm)", href):
            continue
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(src["url"], href)
        if not href.startswith("http"):
            continue
        pub = find_date(re.sub(r"<[^>]+>", " ", seg)) or date_from_url(href)
        seen.add(href)
        out.append({"title": title[:160], "url": href, "published": pub, "summary": ""})
    return out


ADAPTERS = {
    "govcn_json": adapt_govcn_json,
    "gd_search": adapt_gd_search,
    "gd_site": adapt_gd_site,
    "html_list": adapt_html_list,
    "edge_list": None,      # 运行时注入，见下
}


# ── 无头浏览器：绕过部委站的 JS 挑战 ─────────────────────────

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
BROWSER_CMDS = ("msedge", "microsoft-edge", "google-chrome", "google-chrome-stable",
                "chromium", "chromium-browser", "chrome")

_EDGE = None


def find_edge() -> str | None:
    """先找 Windows 上的固定路径，再退回 PATH（Linux/CI 用 chrome/chromium）。"""
    global _EDGE
    if _EDGE is None:
        hit = next((p for p in EDGE_CANDIDATES if os.path.exists(p)), "")
        if not hit:
            hit = next((shutil.which(c) for c in BROWSER_CMDS if shutil.which(c)), "")
        _EDGE = hit or ""
    return _EDGE or None


def edge_dom(url: str, budget: int = 7000) -> str:
    """用无头浏览器渲染页面后取 DOM。人社部等站有 JS 挑战，只能这样拿。"""
    exe = find_edge()
    if not exe:
        raise RuntimeError("未找到 Edge/Chrome/Chromium，edge_list 源不可用")
    proc = subprocess.run(
        [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
         "--hide-scrollbars", f"--virtual-time-budget={budget}", "--dump-dom", url],
        capture_output=True, timeout=180,
    )
    raw = proc.stdout or b""
    if not raw:
        raise RuntimeError(f"headless 返回空 {url}")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "ignore")


def adapt_edge_list(src, conf):
    """渲染后的 DOM 里抓列表，规则同 html_list，可限定同域链接。"""
    html = edge_dom(src["url"], int(src.get("budget", 7000)))
    base = src["url"]
    allow = src.get("allow_host")
    out, seen = [], set()

    for chunk in re.split(r"<li[\s>]", html):
        seg = chunk[:1800]
        a = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
        if not a:
            continue
        href, title = a.group(1), clean_text(a.group(2))
        if not title or len(title) < 8 or href in seen:
            continue
        if href.startswith("javascript:") or href.startswith("#"):
            continue
        from urllib.parse import urljoin
        full = urljoin(base, href) if not href.startswith("http") else href
        if allow and not re.search(allow, full):
            continue
        if not re.search(r"\.(s?html?|jsp|aspx?)$", full):
            continue
        seen.add(href)
        pub = find_date(re.sub(r"<[^>]+>", " ", seg)) or date_from_url(full)
        out.append({"title": title[:160], "url": full, "published": pub, "summary": ""})
    return out


ADAPTERS["edge_list"] = adapt_edge_list


CONTENT_MARKERS = (
    'id="UCAP-CONTENT"', 'class="TRS_Editor"', 'class="article-content"',
    'class="article"', 'id="zoom"', 'class="zoom"', 'id="content"',
    'class="content"', 'class="view"', 'class="detail-content"',
    'class="article_content"', 'id="article"', 'class="conTxt"',
)


def fetch_detail(url: str, title: str = "") -> str:
    """抓正文，用来找截止日期、写摘要。失败不影响主流程。"""
    try:
        html = http_get(url, timeout=20)
    except Exception:
        return ""
    html = re.sub(r"(?is)<(script|style|noscript|iframe)[^>]*>.*?</\1>", " ", html)

    idx = -1
    for mk in CONTENT_MARKERS:
        i = html.find(mk)
        if i >= 0:
            idx = i
            break
    if idx >= 0:
        gt = html.find(">", idx)
        seg = html[gt + 1: gt + 16000] if gt > 0 else html[idx:]
    else:
        seg = html

    txt = clean_text(seg)
    txt = re.sub(r"https?://\S+", "", txt)
    txt = re.sub(r"(网站无障碍开关|-->|字号[:：]?默认? ?大 ?中 ?小|\|)", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()

    # 政务页会把标题、面包屑、导航重复好几遍：定位「后面还有正文」的那次出现
    if title:
        hint = re.sub(r"\s+", "", title)[:16]
        best = None
        for m in re.finditer(re.escape(hint), txt):
            if len(txt) - m.start() >= 300:
                best = m.start()
                break
        if best is not None:
            txt = txt[best + len(hint):]

    txt = re.sub(r"^(来源[:：].{0,20}|发布日期[:：].{0,24})", "", txt)
    txt = re.sub(r"^(扫一扫在手机打开当前页|打印本页|分享到|分享)[^。]{0,40}", "", txt)
    return txt.strip()[:6000]


# ══════════════════════════════════════════════════════════════
# 2. 规则分析引擎
# ══════════════════════════════════════════════════════════════

SIGNAL_RULES = [
    ("征求意见", r"(征求意见|公开征询|意见建议|听证)", 4),
    ("补贴申报", r"(申报|补贴|资助|奖励|扶持|专项资金|兑现|遴选|入库|支持措施|若干措施|资助计划|申请指南)", 3),
    ("人事任免", r"(任免|任命|免去|选举|辞职|干部任前)", 4),
    ("行政送达", r"(送达|认定工伤|注销|遗失|催告|处罚决定书|责令)", 5),
    ("项目公示", r"(建设工程规划许可证|总平面图|宗地|选址方案|规划条件|竣工验收|用地批准|施工许可)", 4),
    ("法规发布", r"(条例|办法|规定|细则|决定|规划|实施方案|指导意见|实施意见)", 3),
    ("公示公告", r"(公示|公告|通告|名单|中标|采购)", 2),
    ("通知发布", r"(通知|通报|函)", 2),
    ("会议活动", r"(会见|出席|致辞|座谈|调研|宣讲|访问|会议|峰会|论坛|开幕|闭幕|茶叙|国事访问)", 2),
]

INDUSTRY_TAGS = {
    "人工智能": r"(人工智能|AI|大模型|智能体|算力|算法|机器人|具身)",
    "半导体": r"(半导体|集成电路|芯片|晶圆|封测|显示面板)",
    "生物医药": r"(生物医药|医疗器械|创新药|临床|大健康|中医药)",
    "新能源": r"(新能源|光伏|储能|锂电|氢能|新能源汽车|充电)",
    "低空经济": r"(低空|无人机|eVTOL|通航)",
    "软件信息": r"(软件|信息服务|数字经济|数据要素|工业互联网|开源|鸿蒙)",
    "金融": r"(金融|基金|保险|证券|跨境资金|QFLP|QDIE|上市|并购)",
    "外贸外资": r"(外贸|进出口|跨境|外资|保税|跨境电商|口岸)",
    "人才": r"(人才|落户|职称|博士后|高层次|引才|安居)",
    "住房": r"(住房|保障房|公租房|安居型|购房|租赁)",
    "教育": r"(教育|学校|学位|高校|职业培训|托育)",
    "医疗": r"(医疗|医院|医保|卫生|疾控|药品)",
    "营商环境": r"(营商环境|放管服|审批|许可|登记|信用|监管)",
    "空间规划": r"(规划|用地|土地|城市更新|整备|产业空间|园区)",
}

REGION_TAGS = {
    "南山": r"(南山|蛇口|粤海)", "福田": r"(福田)", "罗湖": r"(罗湖)",
    "宝安": r"(宝安)", "龙岗": r"(龙岗)", "龙华": r"(龙华)", "光明": r"(光明)",
    "坪山": r"(坪山)", "盐田": r"(盐田)", "大鹏": r"(大鹏)", "深汕": r"(深汕)",
    "前海": r"(前海)", "河套": r"(河套)", "广东省": r"(广东省|全省)", "国家层面": r"(国务院|全国|中共中央)",
}

DEADLINE_CTX = (r"(?:截止|申报时间|申请时间|受理时间|报名时间|反馈|公示期|公示时间|"
                r"有效期|执行期|期间|之日起|至)")
DEADLINE_RE = re.compile(
    DEADLINE_CTX + r"[^。；;\n]{0,40}?(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})"
)
DEADLINE_RE2 = re.compile(r"至\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*[日号]?")


def extract_deadline(text: str) -> str | None:
    """挑出最晚的一个有效截止日（今天起未来 400 天内）。"""
    if not text:
        return None
    today = date.fromisoformat(today_str())
    limit = today + timedelta(days=400)
    cands: list[str] = []

    for m in DEADLINE_RE.finditer(text):
        d = norm_date(*m.groups())
        if d:
            cands.append(d)

    # 省略年份的写法：先按今年算，今年已过才顺延一年，避免凭空多出一年
    for m in DEADLINE_RE2.finditer(text):
        mo, dd = int(m.group(1)), int(m.group(2))
        d = norm_date(today.year, mo, dd)
        if not d or date.fromisoformat(d) < today:
            d = norm_date(today.year + 1, mo, dd)
        if d:
            cands.append(d)

    ok = [d for d in cands
          if today <= date.fromisoformat(d) <= limit]
    return max(ok) if ok else None


def lead_sentence(raw: str, title: str = "") -> str:
    """从公文正文里抽出第一句有效内容，跳过文号、主送机关。"""
    if not raw:
        return ""
    t = raw
    t = re.sub(r"^[\s\S]{0,30}?〔\s*20\d{2}\s*〕\s*\d+\s*号", "", t)
    t = re.sub(r"^[^。]{0,10}?\d{4}\s*年.{0,10}?第\s*\d+\s*号", "", t)
    t = re.sub(r"^[^。]{0,160}?[：:]\s*", "", t)
    t = re.sub(r"^(附件|主题词|抄送|印发|发布|索引号)[\s\S]*$", "", t)
    for p in re.split(r"[。！？；]", t):
        p = p.strip(" ，、\u3000")
        if len(p) >= 12 and not UI_NOISE.search(p) and not re.match(r"^(各地|各省|各区|市直|市各|有关)", p):
            return p[:130]
    return (title or "")[:100]


def classify(title: str, body: str = "") -> str:
    """先只看标题定性；标题没有信号才看正文，避免正文杂词污染分类。"""
    for scope in (title, f"{title} {body}"):
        best, best_score = None, 0
        for name, pat, w in SIGNAL_RULES:
            hits = len(re.findall(pat, scope))
            if not hits:
                continue
            score = w * min(hits, 3)
            if score > best_score:
                best, best_score = name, score
        if best:
            return best
    return "其他动态"


ACTIONS = {
    "补贴申报": "核对申报条件与主体资格，倒排材料清单，卡住截止时间提交；错过窗口通常要等下一批。",
    "征求意见": "公示期内可提交书面意见；定稿前是最容易影响条款的窗口，关注最终版本的差异。",
    "法规发布": "评估对现有业务与合规的直接影响，安排内部解读，梳理需要调整的流程和合同条款。",
    "公示公告": "核对名单中是否有自身或关联方；有异议的，在公示期内按程序提出。",
    "项目公示": "具体地块/项目的规划许可公示，只有涉及其地块或周边时才需要看。",
    "通知发布": "看通知要求谁、做什么、什么时候之前完成；有报送或落实动作的，先排进日程。",
    "行政送达": "针对特定当事人的程序性送达，一般与自身无关，除非你就是当事人。",
    "会议活动": "了解精神与信号，判断后续配套政策方向；不必逐条处理。",
    "人事任免": "关注分管领域与分工变化，留意后续政策口径调整。",
    "其他动态": "归档关注，判断是否与自身业务相关。",
}

IMPACTS = {
    "补贴申报": "直接影响{tags}主体的资金可得性，涉及申报窗口、材料口径与兑现节奏。",
    "征求意见": "若定稿落地，将改变{tags}领域的规则边界，早期反馈成本最低。",
    "法规发布": "对{tags}相关经营行为形成新的合规约束或政策依据，需逐条对照。",
    "公示公告": "涉及{tags}具体主体资格与结果确认，属事实性信息。",
    "项目公示": "单个建设项目的规划/施工许可，影响范围限于该地块及周边，非相关方可略过。",
    "通知发布": "对{tags}主体可能有具体的报送、整改或参与要求，属操作性事项。",
    "行政送达": "程序性送达，影响对象单一，非当事人可忽略。",
    "会议活动": "反映{tags}方向上的政策取向，属信号类信息。",
    "人事任免": "影响部门执行风格与分工，属间接影响。",
    "其他动态": "与{tags}相关，影响程度待正文确认。",
}


def rule_analyze(item: dict) -> dict:
    grp = item.get("grp") or item.get("group") or ""
    body = item.get("raw") or ""
    title = item["title"]
    text = f"{title} {body[:1500]}"

    signal = classify(title, body)

    tags = [k for k, p in INDUSTRY_TAGS.items() if re.search(p, text, re.I)]
    tags += [k for k, p in REGION_TAGS.items() if re.search(p, text)]
    tags = list(dict.fromkeys(tags))[:5]

    imp = 3
    if signal == "补贴申报":
        imp = 5 if re.search(r"(专项资金|扶持|资助|奖励|若干措施)", text) else 4
    elif signal == "法规发布":
        imp = 5 if re.search(r"(条例|实施细则|决定|规划|若干措施)", title) else 4
    elif signal == "征求意见":
        imp = 4
    elif signal in ("公示公告", "通知发布"):
        imp = 3
    elif signal == "项目公示":
        imp = 2
    elif signal == "行政送达":
        imp = 1
    elif signal == "人事任免":
        imp = 2
    elif signal == "会议活动":
        imp = 1 if grp == "媒体" else 2
    if grp != "媒体" and re.search(r"(APEC|十五五|政府工作报告|国务院常务会议|专项资金|申报指南)", text):
        imp = max(imp, 4)
    # 栏目名本身就说明分量：规章/规范性文件/政府文件优先
    col = item.get("col") or ""
    if col and re.search(r"(规章|规范性文件|市政府|政府文件|条例)", col):
        imp = max(imp, 4)
    if col and re.search(r"(政策解读|解读)", col):
        imp = min(imp, 3)
    imp = min(imp, 5)

    dl = extract_deadline(body[:6000]) or extract_deadline(title)

    tag_txt = "、".join(tags[:3]) if tags else "相关领域"
    summary = lead_sentence(body, title)
    if len(summary) < 12:
        summary = title

    return {
        "signal": signal,
        "tags": tags,
        "imp": imp,
        "deadline": dl,
        "summary": summary,
        "impact": IMPACTS[signal].format(tags=tag_txt),
        "action": ACTIONS[signal],
        "engine": "规则引擎",
    }


# ══════════════════════════════════════════════════════════════
# 3. 可选：大模型增强分析
# ══════════════════════════════════════════════════════════════

LLM_SYS = (
    "你是中国政策研究员，服务对象是深圳的创业者/企业经办人。"
    "给你若干条政策条目（标题+正文摘录+来源），逐条输出 JSON。"
    "字段：summary(一句话说清这条到底讲了什么，40字内)、"
    "impact(对这类主体的具体影响，60字内，要具体不要空话)、"
    "action(下一步该做什么，40字内，可执行)、"
    "importance(1-5整数，5=必须马上处理)、deadline(YYYY-MM-DD 或 null)、"
    "tags(2-4个短标签)。只输出 JSON 数组，不要任何解释文字。"
)


def user_config() -> dict:
    """
    读用户配置：本地 config.json 优先，环境变量 CONFIG_JSON 覆盖（给 GitHub Actions 用，
    这样 API Key 和推送地址可以放 Secret，不必提交到仓库）。
    """
    cfg: dict = {}
    p = ROOT / "config.json"
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            cfg = {}
    raw = os.environ.get("CONFIG_JSON")
    if raw:
        try:
            env = json.loads(raw)
            if isinstance(env, dict):
                for k, v in env.items():
                    if k == "notify" and isinstance(v, dict):
                        cfg.setdefault("notify", {}).update(v)
                    else:
                        cfg[k] = v
        except Exception as e:
            print(f"  ! CONFIG_JSON 解析失败：{e}")
    return cfg


def llm_config():
    cfg = user_config()
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or cfg.get("api_key")
    if not key:
        return None
    return {
        "key": key,
        "base": cfg.get("base_url") or os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1",
        "model": cfg.get("model") or os.environ.get("LLM_MODEL") or "deepseek-chat",
    }


def llm_analyze(items: list[dict], cfg) -> dict[str, dict]:
    """批量调用；任何异常都静默回落到规则引擎。"""
    import urllib.request

    payload = [
        {"i": n, "title": it["title"],
         "text": (it.get("raw") or it.get("summary") or "")[:800],
         "source": it.get("source_name", "")}
        for n, it in enumerate(items)
    ]
    body = json.dumps({
        "model": cfg["model"],
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": LLM_SYS},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        arr = json.loads(text)
        out = {}
        for row in arr:
            i = row.get("i")
            if isinstance(i, int) and 0 <= i < len(items):
                out[items[i]["id"]] = row
        return out
    except Exception as e:
        print(f"    ! 大模型分析失败，回落到规则引擎：{e}")
        return {}


# ══════════════════════════════════════════════════════════════
# 4. 存储
# ══════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS policies(
  id TEXT PRIMARY KEY, source_id TEXT, source_name TEXT, grp TEXT,
  title TEXT, url TEXT, published TEXT, raw TEXT,
  first_seen TEXT, last_seen TEXT,
  signal TEXT, tags TEXT, imp INTEGER, deadline TEXT,
  summary TEXT, impact TEXT, action TEXT, engine TEXT, col TEXT
);
CREATE INDEX IF NOT EXISTS idx_pub ON policies(published);
CREATE TABLE IF NOT EXISTS runs(ts TEXT, source_id TEXT, ok INTEGER, n INTEGER, note TEXT);
"""


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(policies)")}
    if "col" not in cols:          # 老库平滑升级
        conn.execute("ALTER TABLE policies ADD COLUMN col TEXT")
        conn.commit()
    return conn


def item_id(url: str, title: str) -> str:
    return hashlib.sha1((url or title).encode("utf-8")).hexdigest()[:16]


def load_conf():
    return json.loads(CONF_PATH.read_text(encoding="utf-8-sig"))


# ══════════════════════════════════════════════════════════════
# 5. 主流程
# ══════════════════════════════════════════════════════════════

def sync(use_ai=True):
    global UA
    conf = load_conf()
    UA = conf.get("user_agent") or UA
    lookback = int(conf.get("lookback_days", 120))
    cap = int(conf.get("max_items_per_source", 40))
    cutoff = date.fromisoformat(today_str()) - timedelta(days=lookback)

    conn = db()
    health, fresh = [], []

    ensure_holidays()

    print(f"\n=== 政策雷达 · 巡检 {now_cst():%Y-%m-%d %H:%M} ===")
    for src in conf["sources"]:
        if not src.get("enabled", True):
            continue
        adapter = ADAPTERS.get(src["adapter"])
        if not adapter:
            print(f"  ? {src['name']}: 未知适配器 {src['adapter']}")
            continue
        try:
            rows = adapter(src, conf)
        except Exception as e:
            print(f"  x {src['name']}: {e}")
            health.append({"name": src["name"], "ok": False, "n": 0})
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?)",
                         (now_cst().isoformat(timespec="seconds"), src["id"], 0, 0, str(e)[:200]))
            continue

        kept = 0
        for r in rows:
            title = r.get("title") or ""
            # 只收中文标题：税务"走出去"指南里混着俄语/英语问答
            if len(CJK_RE.findall(title)) < 3:
                continue
            # 采购公告、政策问答（"……是多少？"）这类不是政策
            if DROP_RE.search(title):
                continue
            # 单源自定义：只要政策味道的标题 / 剔除领导活动
            kt = src.get("keep_title")
            if kt and not re.search(kt, title):
                continue
            dt = src.get("drop_title")
            if dt and re.search(dt, title):
                continue
            pub = r.get("published")
            if pub:
                try:
                    if date.fromisoformat(pub) < cutoff:
                        continue
                except ValueError:
                    pass
            pid = item_id(r["url"], r["title"])
            exists = conn.execute("SELECT 1 FROM policies WHERE id=?", (pid,)).fetchone()
            conn.execute("""
              INSERT INTO policies(id,source_id,source_name,grp,title,url,published,raw,first_seen,last_seen,col)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen
            """, (pid, src["id"], src["name"], src["group"], r["title"], r["url"],
                  pub or "", r.get("summary") or "",
                  now_cst().isoformat(timespec="seconds"),
                  now_cst().isoformat(timespec="seconds"),
                  r.get("extra") or ""))
            kept += 1
            if not exists:
                fresh.append(pid)
            if kept >= cap:
                break

        print(f"  ok {src['name']}: {kept} 条")
        health.append({"name": src["name"], "ok": True, "n": kept})
        conn.execute("INSERT INTO runs VALUES(?,?,?,?,?)",
                     (now_cst().isoformat(timespec="seconds"), src["id"], 1, kept, ""))
    conn.commit()

    # ── 补抓正文：只有拿到正文，截止日期和摘要才有意义 ──
    enrich_limit = int(conf.get("enrich_limit", 30))
    cands = conn.execute("""
        SELECT id, url, title FROM policies
        WHERE (raw IS NULL OR length(raw) < 120) AND url <> ''
        ORDER BY CASE WHEN grp IN ('深圳','广东') THEN 0 ELSE 1 END,
                 length(coalesce(raw,'')), published DESC
        LIMIT ?
    """, (enrich_limit,)).fetchall()
    if cands:
        print(f"\n=== 补抓正文 {len(cands)} 篇 ===")
        got = 0
        for r in cands:
            body = fetch_detail(r["url"], r["title"])
            if len(body) >= 80:
                conn.execute("UPDATE policies SET raw=? WHERE id=?", (body, r["id"]))
                got += 1
            time.sleep(0.3)
        conn.commit()
        print(f"  · 成功 {got} 篇")

    # ── 分析：只补没分析过的，避免重复烧 token ──
    todo = conn.execute(
        "SELECT * FROM policies WHERE signal IS NULL OR signal='' ORDER BY published DESC"
    ).fetchall()
    print(f"\n=== 分析 {len(todo)} 条新条目 ===")

    results: dict[str, dict] = {}
    cfg = None if not use_ai else llm_config()
    if cfg:
        print(f"  · 使用大模型：{cfg['model']} @ {cfg['base']}")
        batch = []
        for row in todo:
            batch.append(dict(row))
            if len(batch) == 8:
                results.update(llm_analyze(batch, cfg))
                batch = []
        if batch:
            results.update(llm_analyze(batch, cfg))
    elif use_ai:
        print("  · 未配置 API Key，使用内置规则引擎（配置方法见 README）")

    for row in todo:
        item = dict(row)
        base = rule_analyze(item)
        ai = results.get(item["id"])
        if ai:
            base.update({
                "summary": ai.get("summary") or base["summary"],
                "impact": ai.get("impact") or base["impact"],
                "action": ai.get("action") or base["action"],
                "deadline": ai.get("deadline") or base["deadline"],
                "engine": f"大模型({cfg['model']})",
            })
            try:
                base["imp"] = max(1, min(5, int(ai.get("importance") or base["imp"])))
            except (TypeError, ValueError):
                pass
            tags = ai.get("tags")
            if isinstance(tags, list) and tags:
                base["tags"] = [str(t)[:12] for t in tags][:5]
        conn.execute("""
          UPDATE policies SET signal=?,tags=?,imp=?,deadline=?,summary=?,impact=?,action=?,engine=?
          WHERE id=?
        """, (base["signal"], json.dumps(base["tags"], ensure_ascii=False), base["imp"],
              base["deadline"] or "", base["summary"], base["impact"], base["action"],
              base["engine"], item["id"]))
    conn.commit()

    print(f"\n本轮新增 {len(fresh)} 条，累计分析完成 {len(todo)} 条。")

    # ── 推送（可选）：只推本轮新增里的重点，最多 10 条 ──
    if fresh and len(fresh) <= 300:
        rows = conn.execute(
            "SELECT * FROM policies WHERE imp>=4 AND id IN (%s) "
            "ORDER BY imp DESC, published DESC LIMIT 10" % ",".join("?" * len(fresh)), fresh
        ).fetchall()
        hot = [dict(r) for r in rows]
        if hot:
            try:
                import notify
                notify.send(hot)
            except Exception as e:
                print(f"  ! 推送失败：{e}")

    render(conn, health)
    conn.close()
    return len(fresh)


# ── 渲染 ─────────────────────────────────────────────────────

def js_safe(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _title_key(t: str) -> str:
    return re.sub(r"[\s《》〈〉\u201c\u201d\"'（）()【】\[\]·,，、:：]", "", t or "")[:40]


def load_view(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM policies WHERE published IS NOT NULL AND published<>''"
    ).fetchall()

    # 同一份文件常被 国务院 / 人民网 / 深圳 同时转发：按标题去重，保留最权威来源
    PRIORITY = {"国家": 4, "深圳": 3, "广东": 2, "媒体": 1}
    best: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        key = _title_key(r["title"])
        cur = best.get(key)
        if cur is None or PRIORITY.get(r["grp"], 0) > PRIORITY.get(cur["grp"], 0):
            best[key] = r

    out = []
    for r in best.values():
        try:
            tags = json.loads(r["tags"] or "[]")
        except Exception:
            tags = []
        out.append({
            "id": r["id"], "title": r["title"], "url": r["url"],
            "date": r["published"], "src": r["source_name"], "g": r["grp"],
            "signal": r["signal"] or "其他动态", "tags": tags,
            "imp": r["imp"] or 3, "deadline": r["deadline"] or None,
            "summary": r["summary"] or "", "impact": r["impact"] or "",
            "action": r["action"] or "", "engine": r["engine"] or "规则引擎",
            "col": r.get("col") or "", "raw": (r["raw"] or "")[:400],
        })
    out.sort(key=lambda x: (x["date"], x["imp"]), reverse=True)
    return out


def load_holidays() -> dict:
    """读 holidays.json，压平成 {日期: {name,type}}。"""
    p = ROOT / "holidays.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    flat = {}
    for _, items in data.items():
        for it in items:
            flat[it["date"]] = {"name": it.get("name") or "", "type": it.get("type") or "off"}
    return flat


def ensure_holidays():
    """节假日表缺失或没覆盖到明年时，从国务院通知重新生成一次。"""
    p = ROOT / "holidays.json"
    need = not p.exists()
    if not need:
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            need = str(now_cst().year + 1) not in data
        except Exception:
            need = True
    if not need:
        return
    try:
        import build_holidays
        build_holidays.main()
    except Exception as e:
        print(f"  ! 节假日表生成失败：{e}")


def render(conn, health=None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load_view(conn)
    tpl = TEMPLATE.read_text(encoding="utf-8")

    if health is None:
        health = [{"name": s["source_name"], "ok": True, "n": 0}
                  for s in conn.execute("SELECT DISTINCT source_id, source_name FROM policies")]

    meta = {
        "today": today_str(),
        "updated": now_cst().strftime("%Y-%m-%d %H:%M"),
        "sources": health,
        "month": today_str()[:7],
        "holidays": load_holidays(),
    }
    html = (tpl.replace("/*__DATA__*/[]", js_safe(data))
               .replace("/*__META__*/{}", js_safe(meta))
               .replace("__UPDATED__", meta["updated"]))
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")

    write_ics(data)
    print(f"  -> {OUT_DIR / 'index.html'}")
    print(f"  -> {OUT_DIR / 'policies.ics'}  ({len(data)} 条)")


def write_ics(data: list[dict]):
    pad = lambda s: str(s).replace("\r", "").replace("\n", "\\n")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//policy-radar//CN//",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:政策雷达", "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    stamp = now_cst().strftime("%Y%m%dT%H%M%SZ")
    for d in data:
        dt = d["date"].replace("-", "")
        desc = "\\n".join([d["summary"], "", "影响：" + d["impact"], "建议：" + d["action"],
                           "来源：" + d["src"], d["url"]])
        lines += [
            "BEGIN:VEVENT",
            f"UID:{d['id']}@policy-radar",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{dt}",
            f"SUMMARY:{pad('★' * d['imp'] + ' ' + d['title'])}",
            f"DESCRIPTION:{pad(desc)}",
            f"URL:{d['url']}",
            f"CATEGORIES:{pad(d['g'] + ',' + d['signal'])}",
        ]
        if d.get("deadline"):
            lines += ["BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
                      f"DESCRIPTION:{pad('明天截止：' + d['title'])}", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    (OUT_DIR / "policies.ics").write_text("\r\n".join(lines), encoding="utf-8")


# ── 命令行 ───────────────────────────────────────────────────

def cmd_show():
    conn = db()
    rows = load_view(conn)
    today = today_str()
    print(f"\n今天 {today} 的条目：")
    hit = [r for r in rows if r["date"] == today]
    if not hit:
        print("  （今日暂无，下面是最近 10 条）")
        hit = rows[:10]
    for r in hit:
        print(f"  [{r['g']}] {r['date']} {'★' * r['imp']} {r['title'][:60]}")
        print(f"      {r['signal']} · {r['src']}")
        if r["action"]:
            print(f"      -> {r['action'][:70]}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="政策雷达：国家/广东/深圳政策第一时间抓取 + 日历")
    ap.add_argument("cmd", nargs="?", default="sync",
                    choices=["sync", "render", "show"], help="默认 sync")
    ap.add_argument("--no-ai", action="store_true", help="不调用大模型，只用规则引擎")
    args = ap.parse_args()

    if args.cmd == "show":
        cmd_show()
    elif args.cmd == "render":
        conn = db()
        render(conn)
        conn.close()
    else:
        t0 = time.time()
        sync(use_ai=not args.no_ai)
        print(f"耗时 {time.time() - t0:.1f}s\n")


if __name__ == "__main__":
    main()
