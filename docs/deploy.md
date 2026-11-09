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
