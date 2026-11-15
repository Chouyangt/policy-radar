# 部署：让手机自己看，不靠电脑开机

三条路，按「省事程度」排。**A 最省事，5 分钟搞定，不用花一分钱。**

---

## A. GitHub Actions 定时跑 + Pages 托管（推荐）

电脑关机也照跑，因为跑在 GitHub 的服务器上。手机打开一个网址就是最新日历。

### 一次性设置

1. 在 GitHub 新建一个**公开**仓库，名字随意，比如 `policy-radar`。
2. 把这个 `policy-radar` 文件夹里的所有东西推到仓库根目录（`.github/` 要一起推）。

```bat
cd policy-radar
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你的用户名>/policy-radar.git
git push -u origin main
```

3. 仓库 **Settings → Pages → Source** 选 `Deploy from a branch`，
   Branch 选 `gh-pages` / `/ (root)`，保存。
4. 仓库 **Settings → Actions → General → Workflow permissions** 选
   **Read and write permissions**（不然存不回数据库）。
5. 到 **Actions** 标签页，点 `政策雷达` → `Run workflow` 手动跑一次。

跑完后你的地址是：

```
日历页   https://<用户名>.github.io/policy-radar/
日历订阅 https://<用户名>.github.io/policy-radar/policies.ics
```

之后每小时自动跑两轮（cron `7,37 * * * *`），手机刷新就是最新的。

### 手机怎么看

**方式一：浏览器**。把上面日历页的地址在手机浏览器打开，加到主屏幕（iOS：分享 → 添加到主屏幕），图标点开就是日历。

**方式二：系统日历订阅**（更 "手机日历" 的感觉）
- iPhone：设置 → 日历 → 账户 → 添加账户 → 其他 → 添加已订阅的日历 → 粘贴 `policies.ics` 的地址
- Android（Google 日历）：电脑打开 calendar.google.com → 左侧「其他日历」+ → 通过网址添加 → 粘贴 ICS 地址
- 订阅后，政策会像日程一样出现在手机原生日历里，`Outlook` 同理

**方式三：消息推送**（最快的 "第一时间"）
在仓库 **Settings → Secrets and variables → Actions → New repository secret** 建一个
`CONFIG_JSON`，内容就是 config.json 的全文（含 `notify` 段），
然后 `radar.py` 里会读环境变量……（见下方「推送」一节，本地跑更简单）

> ⚠️ Pages 是公开的，你的日历页谁拿到地址都能看。只是政策信息，一般无所谓；
> 介意的话走 B 路线，用带密码的托管。

### 如果 Actions 抓不到国内政务站

GitHub 的机器在境外，个别政务站可能对境外 IP 限流。判断方法：
跑完去 Actions 日志里看每个源的输出，如果 `search.gd.gov.cn` 那批全是 `x`（失败），
说明被挡了，走 B 路线。日志会明确列出每个源的成败和条数。

---

## B. 国内云函数（稳定，需实名）

腾讯云函数 SCF 或阿里云函数计算 FC，免费额度足够。

1. 新建函数，运行环境选 Python 3.12，上传整个 `policy-radar` 目录（打成 zip）。
2. 触发器选**定时触发器**，cron 写 `0 */1 * * * *`（每小时）。
3. 执行入口：`radar.main`（或命令行 `python radar.py sync`）。
4. 生成物 `out/index.html` 和 `out/policies.ics` 传到**对象存储 COS/OSS**，
   开静态网站，得到两个公网地址。
5. 手机按 A 的方式二订阅，或直接浏览器打开。

优点：国内网络，抓政务站不会被挡。缺点：要实名、要配密钥，20 分钟起。

---

## C. 家里路由器 / NAS / 旧手机常驻（不推荐但可行）

如果家里有 24 小时开着的 NAS、软路由或旧安卓机：
- NAS/软路由：装 Docker，跑 `python radar.py sync` 的定时任务，再用内网穿透（frp/cloudflare tunnel）暴露 80 端口
- 旧安卓机：装 Termux，`pkg install python`，`crontab` 定时跑，用 `python -m http.server` 提供页面

本质还是"要有个机器一直开着"，只是不一定是你电脑。**不如 A 省事。**

---

## 推送：比刷网页更快

`config.json` 里加 `notify` 段，抓到新的 ★4+ 政策就推到手机上：

```json
{
  "notify": {
    "wecom": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    "telegram_token": "123456:ABC",
    "telegram_chat": "你的chat_id",
    "bark": "https://api.day.app/你的key",
    "site_url": "https://<用户名>.github.io/policy-radar/"
  }
}
```

配哪个发哪个，可以不只配一个：

| 通道 | 怎么拿地址 |
|---|---|
| 企业微信 | 群 → 右上角 → 群机器人 → 添加 → 复制 Webhook |
| 钉钉 | 群设置 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook |
| 飞书 | 群设置 → 群机器人 → 添加 → 自定义机器人 → 复制 Webhook |
| Telegram | 找 @BotFather 建 bot 拿 token，再找 @userinfobot 拿 chat_id |
| Bark（iOS） | App Store 装 Bark，首页就有你的推送地址 |

测一下通不通：`python notify.py`

---

## 两个小提醒

1. **首次跑会灌入几百条历史**，不会推送（超过 300 条时自动跳过推送，避免刷屏）。
2. **数据库要跟着仓库走**，否则每次都是空库重抓、去重失效。
   workflow 里已经带了 `git add -f data/policies.db` 那一步。
   如果你不想要历史积累，删掉那一步即可。
