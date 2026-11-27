# 投稿上线：一条视频从脚本到 B 站（v0.20）

这条线最麻烦的部分不是「怎么点按钮」，是三个不出声的坑：简介被静默丢弃、登录态每个
会话独立、合集功能有门槛。三个都是我自己撞出来的，写在这里免得再撞。

## 一、整条流水线长什么样

```
out/queue/epNN/slides.json   ← 文案（六页，含口播 say）
        │  tools/make_video.py        edge-tts 配音 → 按语音时长定页长 → 逐帧 PNG → x264
        │  tools/make_cover.py        2560×1440，画 4:3 安全区再排版
        ▼
out/epNN.mp4 + out/epNN_cover.png
        │  scripts/prep_next.py       推到服务器 /opt/group-brain/out/，再经 AstrBot 发 QQ
        ▼
他手机上看一眼：可以 / 不行
        │  tools/publish_bsk.py       bsk 驱动他自己的 Edge（投稿号已扫码登录）
        ▼
member.bilibili.com 投稿页 → 立即投稿 → 「稿件投递成功」
        │  member.bilibili.com/x/web/archives  取 bvid 写回 out/queue/state.json
```

为什么不让服务器直传：`GET https://member.bilibili.com/preupload` 恒回
`{"OK":0,"code":601,"info":"您上传视频过快…"}`——上传风控要人验证码，绕不过，也不该绕。

为什么视频给 QQ 只能走 URL：AstrBot 和 snowluma 是两个容器，不共享文件路径，传本地路径
直接 `ENOENT`；而且**视频段必须是消息里唯一的一段**（`retcode=1400 message element
"video" must be the only segment`），所以是「先发视频，再单独发一条文字」。

## 二、坑一：简介会被静默丢弃（最坑）

Quill 富文本编辑器上，下面两种写法**页面全都正常**：字数计数会涨、字也在屏幕上。

```js
// ❌ 这两个都不算数
inst.setText(desc);                       // 只改 DOM
document.execCommand('insertText', ...);  // 在 B 站这里返回 false
```

但提交时 B 站的表单模型里没有这段文字 —— 结果是：**稿件发布成功、简介为空**，
而且你当场看不出来（页面上字明明在）。EP01/EP02 两期都是这么丢的，
事后用 `member.bilibili.com/x/web/archives` 读 `Archive.desc` 才确认 `desc_len=0`。

唯一可靠的写法是让它「真打字」：

```powershell
bsk fill --session <sess> --ref <简介编辑器的 VOM ref> --value "<简介正文>"
```

注意 `.ql-editor` 选择器会匹配到 **2 个**元素（一个隐藏的），所以只能用 VOM ref
（VOM 里长得像 `@e651 textbox "填写更全面的相关信息…"`）。

补旧稿：`python tools/fill_desc.py --aid <AID> --desc-file desc.txt --session <sess>`，
或者发布新片时顺手带上：

```powershell
python tools/publish_bsk.py --meta out/queue/ep03/meta.json --video-url ... --cover-url ... `
    --fix-desc 117626638435953=out/queue/ep01/desc.txt
```

**验证方式**永远是读接口，不是看页面：

```powershell
python _desc_check.py     # 打 BV / desc_len / tags
```

## 三、坑二：登录态是「每个会话一份」

- bsk 的扫码登录结果活在**当前会话**里。`bsk session stop <id>` 之后新建会话，
  打开投稿页直接落回 `passport.bilibili.com/login` —— 我的投稿号 cookie 还在，
  但浏览器里那个 HttpOnly 的 `SESSDATA` 没法用 JS 写进去（Chrome 会忽略同名写入），
  所以也不能靠「把 cookie 注进新会话」续命。
- 会话本身还有 idle 上限：daemon 默认 5 分钟就杀，必须起成
  `bsk daemon start --foreground --session-idle 90m --daemon-idle 12h`。
- 结论：**每天发布都需要他扫一次码**。这不是没做完，是设计上就该有人在场。

## 四、坑三：合集现在开不了

投稿页「加入合集」区域原文：`开通合集功能需满足权益中心等级达 Lv2`。
合集 2024-02-28 起对**电磁力 Lv2** 开放，开通电磁力要 ①有自制稿件 ②粉丝 ≥100 ③信用分 ≥60。
现在 4 粉，所以：

- `member.bilibili.com/x2/creative/web/season/*` 三个路径全 404；
- `api.bilibili.com/x/polymer/web-space/seasons_series_list?mid=3546624852756939`
  能查，`total: 0`（确实一个合集都没有）。

到 100 粉之后的做法：建一个合集「从零做 AI 应用」，按 EP 顺序塞进去，
再把 `--season` 加进 `publish_bsk.py`。在那之前用简介互链代替（每期简介底部都有系列目录）。

## 五、坑四：TTS 走 IPv6 会直接失败

`speech.platform.bing.com` 在这台机器上会先解析到 IPv6（`2620:1ec:33::10`），
而 IPv6 出口是坏的，python 里报 `WinError 10054` 或
`指定的网络名不再可用`；而 PowerShell 的 `Test-NetConnection` 走 IPv4，显示「通」，
非常容易误判成「网络没问题」。

`tools/make_video.py` 里的 `_prefer_ipv4()` 把 `socket.getaddrinfo` 过滤成只返回 A 记录，
在调用 edge-tts 之前装上。别再删。

## 六、文件与命令

| 文件 | 干什么 |
| --- | --- |
| `out/queue/epNN/{slides.json,meta.json}` | 文案：六页内容 + 标题/简介/标签/分区/声明/封面文案 |
| `tools/make_video.py` | 出片。`--still` 只出静态版（1.7MB，不能发片，只用来对版式） |
| `tools/make_cover.py` | 出封面，内含 4:3 安全区 |
| `scripts/prep_next.py` | 挑今天该备的片 → 渲染 → 推服务器 → 发 QQ（`--force ep03` / `--dry`） |
| `tools/publish_bsk.py` | 全流程投稿（`--skip-login` / `--login-only` / `--dry` / `--fix-desc`） |
| `tools/fill_desc.py` | 只补某个旧稿件的简介 |
| `out/queue/state.json` | 已发布的 `{ep: bvid}`，幂等靠它 |

```powershell
# 今天该备的片（13:00 计划任务跑的就是这个）
python scripts/prep_next.py
# 明确备某一期
python scripts/prep_next.py --force ep03
# 投稿（扫码后）
python tools/publish_bsk.py --meta out/queue/ep03/meta.json `
    --video-url "https://zkysever.com/out/ep03.mp4?k=$env:OUT_TOKEN" `
    --cover-url "https://zkysever.com/out/ep03_cover.png?k=$env:OUT_TOKEN" `
    --session <sess>
# 只看投稿前那一屏，不提交
python tools/publish_bsk.py ... --dry
```

## 七、已经出过的片

| 期 | 标题 | BV |
| --- | --- | --- |
| EP01 | 我给一个 QQ 群装上了记忆 | BV1F6sN6mEdm |
| EP02 | 它记住了我们上周说的话 | BV1FDsu64EMn |

EP01 的简介当时被 Quill 吞了，等下次登录用 `--fix-desc` 补。


## 八、改成持久会话 + CDP 发布（2026-11-27，不再需要扫码）

以前每次发片都要他扫一次码，因为 bsk 的 Agent 窗口是**无痕 profile**，登录态不落盘。
现在改成自己起一个带 `--user-data-dir` 的 Edge，登录一次就一直记住：

```
msedge.exe --user-data-dir=C:\Users\Administrator\.dsh\edge-pub ^
           --remote-debugging-port=9223 --remote-allow-origins=* ^
           --no-first-run --no-default-browser-check --no-proxy-server
```

`tools/edge_cdp.py` 负责连它（`launch / whoami / nav / js / set_file / new_tab`），
`tools/publish_cdp.py` 负责发一条：

```
python tools/publish_cdp.py --meta out/queue/ep08/meta.json ^
       --video out/ep08.mp4 --cover out/ep08_cover.png
```

整条链路：真实选文件（`DOM.setFileInputFiles` 指本地 mp4，不用再走服务器 URL）→ 等「上传完成」→
标题（原生 setter）→ 简介（`.ql-editor` 聚焦 + `Input.insertText`）→ 分区 → 创作声明 →
清标签（循环点 ×，一次点不掉全部）→ 加标签（逐个回车）→ 封面（`添加封面` 弹窗里选 png，点「完成」）→
立即投稿 → 页面出现「投稿成功」→ 回读 `x/web/archives` 拿 bvid。

### 四个坑（都写进代码了）

1. **页面卡主线程时 `Runtime.evaluate` 返回 None 而不是报错** —— `wait_uploaded()` 要「读空就重试 3 次再等 5 秒」，
   否则会误判「上传超时」，而其实视频早传完了。
2. **分区不能按全页文字找** —— 「推荐标签」里也有「人工智能」，会点错（第一次点成了「音乐」）。
   必须限定 `.drop-list-v2-item` 里 `innerText` 精确匹配，并且派发的 `MouseEvent` 要带 `clientX/clientY`。
3. **清标签不能一次点完** —— 十个 × 一次点只生效一个。改成循环「点第一个 → 等 0.45s → 再读」，直到清空。
4. **顺序有讲究** —— 改分区会让 B 站自动补「影视剪辑 / 爱情」，所以必须 分区 → 声明 → 清标签 → 加标签。

### 已知不一致（不影响观看）

`x/web/archives` 回来的 `tid` 有两套值（209 / 31），`tname` 接口一律不给。
按他「分区不用管」的口径，没有回改；真要改，走编辑页改完再「立即投稿」会重新进审核。
