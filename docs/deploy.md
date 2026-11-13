# 部署笔记（zkysever.com 是怎么跑到线上的）

一句话：**代码在阿里云服务器上跑，Cloudflare 的隧道把域名接进去，全程不开任何入站端口。**

```
手机 / 浏览器
   │  https://zkysever.com
   ▼
Cloudflare 边缘（DNS、TLS、缓存）
   │  cloudflared 隧道（出站长连接，不用开防火墙端口）
   ▼
服务器 127.0.0.1:8000  ← uvicorn 跑 group-brain
```

## 一、服务器上的东西在哪

| 东西 | 位置 |
| --- | --- |
| 代码 | `/opt/group-brain` |
| Python 环境 | `/opt/group-brain/.venv`（系统 Python 3.10.12） |
| 数据库 | `/opt/group-brain/data/group_brain.db` |
| 表情包图片 | `/opt/group-brain/meme-packs/<pack>/memes/...` |
| 应用服务 | `systemd` 单元 `group-brain.service` |
| 隧道服务 | `systemd` 单元 `cloudflared.service` |
| 隧道配置 | `/etc/cloudflared/config.yml` |
| 隧道日志 | `/var/log/cloudflared.log` + `journalctl -u cloudflared -f` |

服务器 `118.178.109.54`，SSH 私钥在**本机** `C:\Users\Administrator\.ssh\zkysever.pem`。

## 二、为什么用隧道而不是开 80/443

1. 阿里云安全组不用放行任何端口，攻击面为零；
2. 不用买证书，Cloudflare 边缘自动签 HTTPS；
3. 大陆机器没 ICP 备案也能先跑起来（个人非经营性用途，风险低，但**别做成收费服务**）。

代价：域名解析托管在 Cloudflare（NS 已从阿里云的 `dns7/dns8.hichina.com` 改成
`jerry.ns.cloudflare.com` / `vita.ns.cloudflare.com`），所以阿里云那边的解析面板以后不用管了。

## 三、日常操作

```bash
# 看应用活着没
systemctl status group-brain
curl -s http://127.0.0.1:8000/api/health

# 改完代码后重发（在本机执行）
scp -i C:\Users\Administrator\.ssh\zkysever.pem -r app scripts web \
    root@118.178.109.54:/opt/group-brain/
ssh -i C:\Users\Administrator\.ssh\zkysever.pem root@118.178.109.54 \
    'systemctl restart group-brain'

# 看隧道连上没有（CONNECTIONS 那一列有数字就是连上了）
ssh ... 'cloudflared tunnel info group-brain'

# 改了隧道配置
ssh ... 'vi /etc/cloudflared/config.yml && systemctl restart cloudflared'
```

两个服务都设了 `enable`，**服务器重启后自动起来**。

## 四、关键参数

- 隧道名 `group-brain`，ID `2a4bf20a-9f04-4c85-8499-69a313b8e187`
- 证书 `/root/.cloudflared/cert.pem`（授权一次就够，别删；删了要重新在浏览器点授权）
- 隧道凭证 `/root/.cloudflared/2a4bf20a-....json`（等同密码，别贴到任何地方）
- DNS：`zkysever.com` 和 `www.zkysever.com` 各一条 CNAME → `2a4bf20a-....cfargotunnel.com`，代理开启（橙云）

## 五、踩过的坑（都是真的疼过）

1. **GitHub 直连下不动**：`cloudflared-linux-amd64` 从 GitHub Releases 下到 5.2 MB 就卡死，40 KB/s。
   后来改成**在本机下 `.deb`，再 scp 上去**，20 MB 12 秒就完事。
2. **官方 apt 源的 pubkey 地址 404**：`/cloudflared/pubkey` 已经失效，加源必失败（`NO_PUBKEY`）。
   别折腾 apt 源，直接下 deb：`pkg.cloudflare.com/cloudflared/pool/main/c/cloudflared/cloudflared_<版本>_amd64.deb`。
3. **`curl -o` 在远端没写进文件**：结果二进制糊了一屏、`dpkg` 报找不到文件。远端下载一律加
   `-s -o 文件 -w "码=%{http_code} 大小=%{size_download}"`，别让响应体落到终端。
4. **`pkill -f cloudflared` 会把自己杀掉**：因为 SSH 那条命令的命令行里也含 “cloudflared”，
   匹配到了自己 → 连接直接断。用 `pkill -x cloudflared`（按进程名精确匹配）。
5. **Windows 的 ssh 会把远端命令里的双引号吃掉**：`grep -Ei "a|b"` 到服务器变成 `grep -Ei a|b`，
   被 bash 当成管道。远端命令尽量不用双引号，需要复杂脚本就 scp 一个 `.sh` 过去跑。
6. **`cloudflared service install` 报 config 冲突**：`/root/.cloudflared/config.yml` 和
   `/etc/cloudflared/config.yml` 同时存在就会拒装。只留一个，用
   `cloudflared --config /etc/cloudflared/config.yml service install`。
7. **表情包路径**：库里原来存的是 `C:\Users\...` 绝对路径，Linux 上必然 404。
   改成存「相对 `MEME_PACKS_DIR` 的路径」，本机和服务器共用同一个库文件。
8. **图片发成 `application/octet-stream`**：Ubuntu 没装 `media-types` 时
   `mimetypes.guess_type('.png')` 返回 `None`。在 `app/main.py` 里自己兜一张小表。

## 六、口令门（不用邮箱的那种）

需求是「给一层验证码，别走邮箱」，所以没用 Cloudflare Access（那套必须邮箱收码），
而是在应用里加了一道 80 行的门：`app/auth.py` + `app/main.py` 里的 middleware + `web/login.html`。

**怎么工作**

1. 任何请求先进 middleware：
   - 公开路径 `/login`、`/api/health`、`/favicon.ico` → 直接放行
   - 其余看 cookie `gb_gate` 是否合法 → 合法放行
   - `/api/*` 不合法回 401 JSON；页面不合法 302 到 `/login`
2. `POST /login` 校验密码，通过后发 cookie：`HttpOnly`、`SameSite=Lax`、
   HTTPS 下带 `Secure`、有效期 7 天。
3. cookie 内容不是随机串，是 `过期时间.HMAC-SHA256(过期时间)`——
   改一个字节就失效；伪造得先拿到 SECRET。

**密码和密钥放哪**

`/opt/group-brain/.env`（`chmod 600`，只有 root 能读，**不进 git**）：

```ini
GATE_PASSWORD=你的口令
GATE_SECRET=64位随机十六进制
```

systemd 通过 `EnvironmentFile=/opt/group-brain/.env` 读进去。
`GATE_SECRET` 不改的话，服务重启用户不用重新输；改了等于把所有人踢下线。

**日常操作**

```bash
# 改口令
ssh ... 'vi /opt/group-brain/.env && systemctl restart group-brain'

# 让所有人重新输（换 SECRET）
ssh ... 'sed -i "s/^GATE_SECRET=.*/GATE_SECRET=$(openssl rand -hex 32)/" /opt/group-brain/.env && systemctl restart group-brain'

# 自己想退出登录：浏览器打开 https://zkysever.com/logout
```

**防爆破**：同一 IP 十分钟内错 10 次就挡住（进程内存计数，重启清零）。
真要抗大规模撞库，得把这层计数放到 Cloudflare 的 Rate Limiting 规则里。

**注意**：这层门保护的是「谁能看内容」，不改变「内容在服务器上」这个事实。
`.env` 泄露 = 门白装，所以它没进 git，也别贴到群里。

---

# 七、线上 500 事故复盘（2026-11-09 晚，v0.5 上线时）

**症状**：页面打得开、口令门正常、`/api/health` 回 200，但 `/api/stats`、`/api/search`、`/api/meme/*` 全部 500。
`journalctl -u group-brain` 里只有一行关键：

```
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread.
The object was created in thread id 139664937698880 and this is thread id 139664911472192.
```

**根因**：`app/main.py` 里原来是一个进程级全局连接（`_conn`）。
FastAPI 的同步接口跑在线程池里，第二个工作线程拿第一条连接用，sqlite 直接拒绝。
本地手测全是单线程，一路绿灯；一上公网并发就集体崩。

**修法**：改成 thread-local，每个工作线程各开一条：

```python
_local = threading.local()

def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = db.connect()
        _local.conn = c
    return c
```

**验证方式**（这一步比修法本身重要）：本地起了服务，用 20 个线程各发 5 个请求（共 100 个，
混着 `/api/search`、`/api/stats`、`/api/meme`），要求 100/100 都是 200。

**顺带修的两件事**：
1. `/api/health` 以前只回 `{"ok": true}` —— 库炸了它照样报健康，等于自己骗自己。
   现在会真的去查一次库，回 `db_ok` / `messages` / `memes` / `vectors`，库坏就 `db_ok: false`。
2. 版本号从 0.1.0 提到 0.5.0，页面角标同步。

**教训**：
- 「本地跑通了」只等于「单线程跑通了」。上线前必须用并发压一遍。
- 探活接口不碰数据库，就不能证明服务是好的。
- 排查时先看 `journalctl`，不要靠猜。这次的 traceback 一次就把范围缩到一行代码。

# 八、连不上服务器的排查顺序（本机网络封 22 端口时）

2026-11-09 晚上遇到一次：`scp` 全部 `Connection closed`，`ssh` 报 `Connection timed out`。
服务器其实好好的（网页一直在线）——是**本机所在网络封了出站 22 端口**。

三步定位，别一上来就怀疑服务器：

```powershell
# 1. 服务器还活着吗？走 443 看网页
curl.exe -s -o NUL -w "%{http_code}`n" https://zkysever.com/api/health

# 2. 本机能不能出 22？拿 GitHub 当对照组（22 和 443 都该通）
Test-NetConnection github.com -Port 22 -InformationLevel Quiet
Test-NetConnection github.com -Port 443 -InformationLevel Quiet

# 3. 只有我们的机器不通、GitHub 的 22 也不通 → 是本地网络，不是服务器
Test-NetConnection 118.178.109.54 -Port 22 -InformationLevel Quiet
```

判定：
- 第 2 步 22=False、443=True → **本地网络封了 22**，换网络（手机热点）即可，别改服务器。
- 第 2 步都正常、只有第 3 步不通 → 才轮到怀疑阿里云安全组 / 云盾拦截 / sshd 挂了。

# 九、SSH 三端口（22 / 2222 / 443）——被封 22 之后的永久解法

## 为什么做这件事

换热点能连，但那是**临时**的：下次回到校园网/运营商宽带，22 又被封，就又得换网络。
真正的原因在**出站方向**，服务器本身没问题，所以解法是把 SSH 挪到一个不会被封的端口。

现在 sshd 同时监听三个端口，任意一个通就能连：

```
Port 22      # 老通道，保留。别删，某些环境只放行它
Port 2222    # 主用。绝大多数封 22 的网络不封它
Port 443      # 保底。443 几乎从不被封（它得让人上网）
```

**配套的安全组入方向**（阿里云控制台 → ECS → 安全组 → 入方向）必须有 `2222/2222` 和
`443/443` 两条 `0.0.0.0/0`，否则端口在监听但外面连不进来。

## 本机配置（`C:\Users\Administrator\.ssh\config`）

```sshconfig
Host gb
    HostName 118.178.109.54
    User root
    IdentityFile ~/.ssh/zkysever.pem
    Port 2222

Host gb443
    ... Port 443
Host gb22
    ... Port 22
```

用法：`ssh gb`（默认 2222）；哪天 2222 也不通，就 `ssh gb443`。

## ⚠️ 踩过的坑（这次把自己锁在外面 5 分钟）

**sshd 的 `Port` 是「覆盖式」的，不是「追加式」的。**
只要配置文件里出现任何一行 `Port xxx`，**默认的 22 就不再监听**——哪怕你只想着「多加一个端口」。

我当时的操作：`echo "Port 2222" >> sshd_config` + `echo "Port 443" >> sshd_config`，
以为「22 是默认的，不用写」。结果 22 当场消失，而安全组只放行 22，
2222/443 又被安全组挡着 → 三个端口全不通，SSH 彻底断。

**正确写法**：三个都显式写出来，一个都别指望默认值。

```bash
for p in 22 2222 443; do
  grep -qE "^Port[[:space:]]+$p$" /etc/ssh/sshd_config || echo "Port $p" >> /etc/ssh/sshd_config
done
sshd -t && systemctl restart ssh     # 先 -t 再重启，语法错就别重启
ss -tlnp | grep sshd                  # 三个端口都在，才算成
```

**如果又锁在外面**（SSH 全不通、安全组也没法改时）：阿里云控制台 → 这台实例 →
「远程连接」→「发送命令」（云助手，走内网 HTTP，不依赖 SSH），粘贴上面那三行即可救回来。
云助手是最后一条命，先把这条记下来，别等用到的时候现查。

## 顺带说明：443 上跑的是 SSH，不是 HTTPS

`https://118.178.109.54` 用浏览器打开会看到证书错误或一串 `SSH-2.0-...`，这是正常的，
**不影响 `zkysever.com`**——官网走的是 cloudflared 隧道，是服务器**主动往外连**，
跟入方向 443 没有任何关系。

---

# 备份（2026-11-10 加的）

## 谁在备份、备份到哪

| 层 | 什么时候 | 放哪 | 留几份 |
|---|---|---|---|
| 服务器自动 | 每天 03:30（crontab） | `/opt/group-brain/data/backups/` | 14 |
| 本机（要手动或自己排计划） | 你跑 `pull_backup.ps1` 时 | `C:\Users\Administrator\Desktop\group-brain-backups\` | 7 |

服务器那条 crontab（`crontab -l` 能看到）：

```
MAILTO=""
SHELL=/bin/bash
HOME=/root
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
20 3 * * * cd /opt/group-brain && set -a && . ./.env && set +a && .venv/bin/python scripts/sync_messages.py >> /var/log/gb-sync.log 2>&1
25 3 * * * cd /opt/group-brain && set -a && . ./.env && set +a && .venv/bin/python scripts/extract_facts.py --new >> /var/log/gb-facts.log 2>&1
30 3 * * * cd /opt/group-brain && .venv/bin/python scripts/backup_db.py >> /var/log/gb-backup.log 2>&1
0 9 * * 1 bash /opt/group-brain/scripts/cron_coach.sh >> /var/log/gb-coach.log 2>&1
0 4 1 * * cd /opt/group-brain && MEME_PACKS=/opt/group-brain/meme-packs .venv/bin/python scripts/restore_check.py >> /var/log/gb-restore.log 2>&1
```

第二条是「每周一 09:00 自动生成上一周的周报」，见下面「定时任务」一节。

## 为什么不是 `cp group_brain.db`

库开着 WAL（`PRAGMA journal_mode=WAL`）。直接 `cp` 只拷主文件，最近的写入还在
`group_brain.db-wal` 里，拷出来的可能是旧的，严重时是不一致的。
`scripts/backup_db.py` 用的是 `sqlite3.Connection.backup()`：它在一个读事务里把一致性
快照写到目标文件，WAL 里的内容也会带上。备份完还会打开备份文件，核对
`messages / memes / vectors` 三张表的行数。

## 怎么确认它真的在跑

2026-11-10 验证过一次：把同一条命令临时改成「每分钟」跑，两分钟后
`data/backups/` 里多出两份文件、日志里有 `[ok] 备份: ... 消息 483 / 表情包 122 / 向量 605`，
然后撤掉了临时任务。**别只相信 crontab 文件存在，要看到文件真的多出来。**

看日志：

```bash
tail -n 20 /var/log/gb-backup.log
ls -lt /opt/group-brain/data/backups/ | head
```

## 还原（出事时照抄）

```bash
systemctl stop group-brain
cd /opt/group-brain
cp data/backups/group_brain-20261110-210408.db data/group_brain.db
rm -f data/group_brain.db-wal data/group_brain.db-shm   # 旧 WAL 必须删，否则启动后会把旧内容盖回去
systemctl start group-brain
curl -s localhost:8000/api/health                        # 看 db_ok 与三张表的条数
```

## 恢复演练：`scripts/restore_check.py`（2026-11-11 加的）

备份写出来了 ≠ 备份能用。这份脚本干的事就是**当场恢复一次给你看**：

```bash
cd /opt/group-brain
.venv/bin/python scripts/restore_check.py                    # 默认挑 data/backups 里最新的一份
.venv/bin/python scripts/restore_check.py data/backups/xxx.db
MEME_PACKS=/opt/group-brain/meme-packs .venv/bin/python scripts/restore_check.py   # 顺带验图在不在
```

它检查六件事：`PRAGMA integrity_check`、五张表齐不齐、三张表的行数、
**FTS 索引条数与正文条数是否一致**（行数对但索引空了，是这套系统最典型的坏法）、
**真跑一次检索**「肥鱼」看有没有结果、抽查一张表情包的图在不在目录里。

三条设计：①永远在 `/tmp` 的副本上演练，不碰备份原件也不碰线上库；
②行数之外必须真查一次，否则测不出「文件好但索引空」；
③失败时退出码非 0，并把临时副本留下给人看。

实测（2026-11-11）：本机与服务器都通过——`integrity_check: ok`、483/122/605、
索引 483 = 正文 483、查「肥鱼」3 条 1.56 ms。

服务器每月 1 号 04:00 自动跑一次（crontab 第三条），日志 `/var/log/gb-restore.log`。
**只验证不修复**：真发现坏了，它只告诉你坏，不会自动去动线上库——那种事应该人看着做。

## 还没做的

- **本机那份要靠手动跑**（`scripts/pull_backup.ps1`）：笔记本不常开，排计划任务也不一定跑得到，所以没替你设。
- `.env` 不在备份里（里面有 `GATE_SECRET` 和几个 key）。它的内容在你本机
  `~/.dsh/secrets/` 和这五个变量里，丢了重新配一次即可，不算数据损失。


# 定时任务（2026-11-10 加的）

服务器上现在有五条 cron，都在 root 的 crontab 里：

| 时间 | 干什么 | 日志 | 脚本 |
|---|---|---|---|
| 每天 03:20 | 从 AstrBot 活性库增量同步群消息，再补新消息的向量 | `/var/log/gb-sync.log` | `scripts/sync_messages.py` |
| 每天 03:25 | 只抽「新消息」出事实草稿，写进 `facts` 表待审 | `/var/log/gb-facts.log` | `scripts/extract_facts.py --new` |
| 每天 03:30 | 备份数据库，留 14 份 | `/var/log/gb-backup.log` | `scripts/backup_db.py` |
| 每周一 09:00 | 生成上一周的周报，写进 `reports` 表 | `/var/log/gb-coach.log` | `scripts/cron_coach.sh` |
| 每月 1 号 04:00 | 拿最新备份做一次恢复演练（只验证不修复） | `/var/log/gb-restore.log` | `scripts/restore_check.py` |

顺序不是随手排的：**先同步（03:20）→ 再抽事实（03:25）→ 最后备份（03:30）**，这样每天早上那份备份里装的是当天最新的数据，事实草稿也是基于当天最新消息抽的。

## 事实抽取为什么是增量的（2026-11-13 改）

第一版每次 `--limit 600` 全量重抽：564 条要 29 批、45 秒、几毛钱，而且**以前抽过的还会再抽一遍**（靠去重挡着，白花钱）。

现在脚本带一个游标 `facts_last_id`（存在 `meta` 表里），`--new` 只读 `id > 游标` 的消息，跑完把游标推到本轮最大 id：

```
[增量] 上次抽到消息 id=573
[完成] 批数 1、耗时 3s；本轮新增 0、重复 0、无效 0
[游标] facts_last_id = 573（下次 --new 从这里往后抽）
```

好处是 cron 那条每天只处理「昨天新来的几十条」，几秒、几分钱。代价是**如果中途换了数据库或清过库，游标要手动清零**（`DELETE FROM meta WHERE k='facts_last_id'`），否则它会一直以为「都是旧的」。

抽出来的还是 `pending`，`/facts` 不点头就不进检索——这条没变。
反过来就是「备份永远比数据晚一天」。

## 周报为什么包一层 `.sh`

`cron_coach.sh` 里只有三件事：`cd` 到项目目录、`set -a; . ./.env; set +a`、然后 exec python。
听起来多余，但两条都是被逼的：

1. **cron 里没有 `DEEPSEEK_API_KEY`**。那个变量是 systemd 通过 `EnvironmentFile=/opt/group-brain/.env`
   注入给服务的，cron 完全不经过 systemd。不 source `.env` 就会报
   `RuntimeError: 没配 DEEPSEEK_API_KEY，写不了周报` —— 我第一次就是这么错的。
2. **crontab 里别写带分号的长链**。`set -a; . ./.env; set +a; python …` 这种，
   从 Windows 上 `ssh` 过去时引号会被剥掉，命令被拆成好几段执行（顺带把整个 shell 环境 `set` 打印了一遍）。

**踩坑：`crontab /tmp/gb.cron` 报 `missing newline before EOF`。** 文件最后一行没有换行符就装不进去。
本地写 `.cron` 时记得 `$t.EndsWith("`n")` 否则补一个——这个错很蠢，但它会让人以为 crontab 坏了。

## 怎么确认周报那条也在跑

跟备份一样，别只看 crontab 文件：把那条临时改成 `* * * * *` 跑一分钟，
然后看 `tail /var/log/gb-coach.log` 有没有 `[ok] 已存入 reports 表`，
再看 `reports` 表的 `generated_at` 有没有变新。验完立刻撤掉探针。

```bash
tail -n 20 /var/log/gb-coach.log
# 看有什么周报了（用一个临时脚本查，别在 ssh 里拼带引号的一行 python）
/opt/group-brain/.venv/bin/python /tmp/count_reports.py
```

## 周报没有「推送」

生成完只落在数据库和 `/coach` 页面上，不会主动发到群里或手机上。原因很实际：
推 QQ 群要接 AstrBot 的接口（能推，但要写插件、还得防刷屏），
推微信要企业微信 webhook（要新的账号），发邮件要 SMTP 授权码（要你的邮箱密码）。
这三条路都要额外账号或会打扰群里，所以先停在「生成好，你自己打开看」这一步。
真要推，最短的路是 QQ 群——让 bot 每周一在群里发一句「上周周报已生成」。


# 数据同步：让库里的群消息自己变新（2026-11-12 加的）

## 以前是怎么更新的（以及为什么不行）

到这里为止，「更新数据」一直是一件**人来干的活**：某个人 SSH 上去把 AstrBot 的
`data_v4.db` 拷下来，放本地 `data/raw/`，再跑一遍 `scripts/import_messages.py` 全量重导。

两个问题叠在一起：

1. **没人做就永远不更新**。2026-11-12 发现库里的时间范围还停在 `2026-11-05 07:12`，一周的消息全没有；
2. **全量重导还是「删库重灌」**——`import_messages.py` 里第一句就是 `DELETE FROM messages`。
   数据一过期，中间那几天就补不回来了；而且每次重导都在删了再写，用户的原话是「别再删它记忆了」。

## 现在怎么更新

```
AstrBot 活性库 /opt/qqbot/astrbot-data/data_v4.db
        │  sqlite backup API（不是 cp，见下）
        ▼
   /tmp/gb_sync_<ts>.db   一致快照
        │  scripts/import_messages.py::import_from()  增量：只新增/只更新变化的
        ▼
   data/group_brain.db     messages + messages_fts
        │  scripts/build_vectors.py --kind message    只算缺的那些
        ▼
   vectors 表补上
```

一条命令：`scripts/sync_messages.py`。服务器每天 **03:20** 自动跑一次（cron），
日志 `/var/log/gb-sync.log`；管理面板上也有一键「同步群消息（增量）」。

## 为什么用 backup API 而不是 `cp`

AstrBot 在跑、库是 **WAL 模式**。`cp data_v4.db` 只拷主库文件，**-wal 里还没合并的那部分会丢**——
而最新的消息恰好就在那里。`sqlite3.Connection.backup()` 会把这些一起带过来，拿到的是一份前后一致的快照。
（备份脚本 `backup_db.py` 也是同一个道理，用的是同一个接口。）

## 增量是怎么判的

对每一条源消息：`(id)` 在库里有没有、`(text, ts)` 变没变。

| 情况 | 动作 |
|---|---|
| 库里没有 | 插入 + 建索引 |
| 有，但 `text`/`ts` 变了（机器人改名、消息重发） | `INSERT OR REPLACE` 那一行 + **先 `DELETE FROM messages_fts WHERE msg_id=?` 再插** |
| 没变 | 跳过，连分词都不做 |

`messages_fts` 是普通 fts5 表（不是 external content），所以改一行必须自己删自己那条，
否则检索会命中一份已经不存在的旧分词。

想回到老行为（整表重灌）加 `--full`，只在数据真的错乱时用。

## 怎么确认它真的在跑

```bash
# 手动跑一次，看新增多少条
cd /opt/group-brain && set -a && . ./.env && set +a && .venv/bin/python scripts/sync_messages.py
# 预期输出：[ok] 原始 N 条 → 新增 x / 更新 y / 没变 z，库里现在 M 条

tail -n 20 /var/log/gb-sync.log      # cron 那条的日志
```

页面上的核对点：管理面板「聊天消息」那个数字有没有变大；`/api/health` 的 `messages` 字段同理。

## 已知限制

- **只同步消息，不同步表情包**。表情包来自本机的两个 pack 目录，跟 AstrBot 无关。
- **同步失败不会重试**。cron 只跑一次，失败就看日志、手动跑一次——比「偷偷重试三次」好排查。
- **没有「最后同步时间」的展示**。现在只能从 `messages` 里最大 `ts` 推断，或者看日志。
  真要看，`SELECT MAX(ts) FROM messages;`。


