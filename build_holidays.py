# -*- coding: utf-8 -*-
"""
从中国政府网抓历年《国务院办公厅关于XXXX年部分节假日安排的通知》，
解析成 holidays.json：
    {"2026": [{"date":"2026-01-01","name":"元旦","type":"off"}, ...]}
type = off 放假 / work 调休上班
"""
import json, re, subprocess, sys, io
from datetime import date, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"


def get(u):
    o = subprocess.run(["curl.exe", "-sL", "--compressed", "--max-time", "25",
                        "-A", UA, u], capture_output=True, timeout=45).stdout or b""
    for e in ("utf-8", "gb18030"):
        try:
            return o.decode(e)
        except UnicodeDecodeError:
            continue
    return o.decode("utf-8", "ignore")


def body_text(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    for mk in ('id="UCAP-CONTENT"', 'class="article"', 'id="content"'):
        i = html.find(mk)
        if i >= 0:
            gt = html.find(">", i)
            html = html[gt + 1: gt + 20000]
            break
    t = re.sub(r"<[^>]+>", "", html)
    t = t.replace("\u3000", " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", t)


MONTH_DAY = r"(\d{1,2})月(\d{1,2})日"
# 日期后面常跟「（周四）」「（农历除夕、周日）」，中间还可能有顿号
MD_OPT = MONTH_DAY + r"(?:[（(][^）)]{0,14}[）)])?"
RANGE_RE = re.compile(MD_OPT + r"\s*(?:至|—|－|-|到)\s*(?:(\d{1,2})月)?(\d{1,2})日")
WORK_RE = re.compile(MD_OPT + r"[、，,]\s*(?:" + MD_OPT + r"[、，,]\s*)*上班")
WORK_ONE = re.compile(MD_OPT + r"\s*上班")


def parse_year(text, year):
    """按「一、二、三…」拆分，每段里取放假区间和调休上班日。"""
    out = []
    # 只在「。」之后切条目，避免把「农历腊月二十八、」里的「八、」误当条目标记
    parts = re.split(r"(?<=。)\s*(?=[一二三四五六七八九十]+、)", text)
    for seg in parts:
        m = re.match(r"\s*[一二三四五六七八九十]+、\s*([^：:]{2,10})[：:](.{0,400})", seg)
        if not m:
            continue
        name = m.group(1).strip()
        body = m.group(2)

        ranges = list(RANGE_RE.finditer(body))
        for rng in ranges:
            m1, d1 = int(rng.group(1)), int(rng.group(2))
            m2 = int(rng.group(3)) if rng.group(3) else m1
            d2 = int(rng.group(4))
            try:
                start, end = date(year, m1, d1), date(year, m2, d2)
            except ValueError:
                continue
            if end < start:
                end = date(year + 1, m2, d2)
            cur = start
            while cur <= end:
                out.append({"date": cur.isoformat(), "name": name, "type": "off"})
                cur += timedelta(days=1)

        # 只放一天、没有「至」的条目
        if not ranges:
            for one in re.finditer(MD_OPT + r"\s*(?:放假|调休)", body):
                try:
                    d = date(year, int(one.group(1)), int(one.group(2)))
                except ValueError:
                    continue
                out.append({"date": d.isoformat(), "name": name, "type": "off"})

        # 调休上班：区分出「……上班」那句，再把句中的日期都收进来
        for sent in re.split(r"[。；]", body):
            if "上班" not in sent:
                continue
            head = sent.split("上班")[0]
            for w in re.finditer(MD_OPT, head):
                try:
                    wd = date(year, int(w.group(1)), int(w.group(2)))
                except ValueError:
                    continue
                out.append({"date": wd.isoformat(), "name": name + "调休", "type": "work"})
    # 去重
    seen, uniq = set(), []
    for i in out:
        k = (i["date"], i["type"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(i)
    return uniq


def main():
    rows = json.loads(get("https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"))
    notices = {}
    for r in rows:
        t = r.get("TITLE", "")
        if "节假日安排" in t:
            m = re.search(r"(20\d{2})年", t)
            if m:
                notices.setdefault(m.group(1), r["URL"])

    result = {}
    for year in sorted(notices):
        text = body_text(get(notices[year]))
        items = parse_year(text, int(year))
        if items:
            result[year] = items
            offs = [i for i in items if i["type"] == "off"]
            print(f"{year}: 放假 {len(offs)} 天，调休 {len(items) - len(offs)} 天 "
                  f"| {', '.join(sorted({i['name'] for i in offs}))}")
        else:
            print(f"{year}: 解析失败，通知正文 {len(text)} 字")
            print("   样本:", text[:200])

    if result:
        io.open("holidays.json", "w", encoding="utf-8").write(
            json.dumps(result, ensure_ascii=False, indent=1))
        print("\n已写入 holidays.json")

    for y in ("2026", "2027"):
        if y in result:
            print(f"\n{y} 明细：")
            for i in result[y]:
                print("  ", i["date"], i["type"], i["name"])


main()
