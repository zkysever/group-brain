# Cloudflare 接入记录

> 这份文件是给你的副本。以后忘了 NS 是什么、配到哪一步了，看这里。

## 账号

| 项 | 值 |
|---|---|
| 平台 | Cloudflare（免费 Free 计划） |
| 账号邮箱 | （你注册时用的邮箱，自己补一下） |
| 已接入域名 | `zkysever.com` |
| 接入时间 | 2026-11-09 |

## 名称服务器（Nameservers）★ 关键

```
jerry.ns.cloudflare.com
vita.ns.cloudflare.com
```

**注意**：这一对是**你账号固定的**，以后再接入别的域名，Cloudflare 给的可能还是这两个。

原来的（阿里云默认，**要删掉**）：

```
dns7.hichina.com
dns8.hichina.com
```

## 进度

- [x] 域名注册（阿里云，2026-11-05，¥76 首年，2027-11-05 到期）
- [x] 域名实名认证（2026-11-09 09:00 通过，状态 active）
- [x] 注册 Cloudflare 账号
- [x] 在 Cloudflare 添加站点 `zkysever.com`（Free 计划）
- [ ] **去阿里云把 NS 改成上面那两个**（当前卡在这）
- [ ] 等 Cloudflare 检测到 NS 生效（通常 5 分钟 – 2 小时，最长 24 小时）
- [ ] 服务器装 cloudflared 并 `tunnel login`
- [ ] 创建 tunnel、绑定域名
- [ ] systemd 开机自启 + 掉线自动重连

## 怎么验证 NS 生效

```powershell
$ns = (Resolve-DnsName zkysever.com -Type NS -Server 8.8.8.8 | Where-Object NameHost).NameHost -join "  "
"$(Get-Date -Format 'HH:mm:ss')  NS = $ns"
```

看到 `jerry.ns.cloudflare.com` 和 `vita.ns.cloudflare.com` 就算生效。
看到 `dns7.hichina.com` 说明还没同步，**继续等，别反复改**。

## 备忘

- 域名到期：**2027-11-05**，建议在手机日历里设 2027-09-05 的提醒
- 本站在国内属"未备案"状态，走 Cloudflare Tunnel 不经过备案体系；个人非经营性用途，别做收费服务
- Cloudflare 免费版够用：无限流量、免费 SSL、免费 DDoS 防护
