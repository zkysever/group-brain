# group-brain

群聊记忆体 · v0.9

把 AstrBot 里攒下来的群聊记录 + 两套表情包，变成**一条能搜的索引**，
再往上加一层「模型自己决定查不查」的 Agent。在线地址：**https://zkysever.com**

检索 = 关键词（SQLite FTS5 + 中文 bigram）与向量（bge-m3）各取 30 条，用 RRF 合成一个排名。
Agent = 把检索包成三个工具（查聊天 / 找表情 / 问时间）交给 DeepSeek function calling，最多 3 轮。

---

## 30 秒跑起来

1. 装 Python 3.12（https://www.python.org/downloads/ ，安装时勾 **Add python.exe to PATH**）
2. 双击 `双击启动.bat`
3. 浏览器会自己打开 `http://127.0.0.1:8000`

第一次跑要装依赖，大概 1–2 分钟。之后再跑就是秒开。
**那个黑窗口别关**，关了服务就停了。

---

## 现在能干什么

| 功能 | 状态 |
|---|---|
| 群聊消息检索（483 条，关键词 + 向量混合） | ✅ v0.4 |
| 表情包按描述/关键词检索（122 张，直接出图） | ✅ |
| 记忆回放：点一下看这条消息前后说了什么 | ✅ v0.6 |
| 中文 bigram 分词（索引与查询同一套切法） | ✅ |
| 检索评测（48 条，Recall@5 1.000 / MRR 0.939） | ✅ `EVAL.md` |
| Agent 模式：模型自己决定调不调工具 | ✅ A2，见 `/agent` |
| Agent 评测（50 条 + 6 条上下文题，49/50、误触发 0/11） | ✅ `EVAL.md` 第七节 |
| 口令门（HMAC cookie + 限速，不走邮箱） | ✅ v0.4 |
| 部署：cloudflared 隧道 + systemd + 三端口 SSH | ✅ `docs/deploy.md` |
| Web 管理面板（重建索引 / 重导表情包 / 建向量 / 合并 WAL） | ✅ v0.7 |
| 作品集页（公开，数字实时） | ✅ v0.8，/portfolio |
| 学习教练：从提交和群活跃度生成周报 | ✅ A4，见 `/coach` 与 `docs/coach.md` |

---

## 目录结构

```
group-brain/
├─ 双击启动.bat          ← 唯一需要双击的东西
├─ app/
│  ├─ config.py          路径 / 端口 / 从 .dsh/secrets 读 key
│  ├─ text.py            中文 bigram 分词（整个项目的检索地基）
│  ├─ db.py              建表 + 连接 + 记忆回放的窗口查询
│  ├─ search.py          FTS5 + 向量，四路 RRF 融合
│  ├─ vectors.py         向量存取与暴力点乘（605 条不值得上向量库）
│  ├─ embeddings.py      bge-m3 接口
│  ├─ agent.py           A2：三个工具 + 最多 3 轮的调用循环
│  ├─ auth.py            口令门：HMAC cookie + 限速
│  ├─ admin.py           管理面板后端：点按钮 = 跑一次脚本（单任务锁 + 抓日志）
│  └─ main.py            FastAPI：页面 + 接口
├─ scripts/
│  ├─ init_db.py         建表
│  ├─ import_memes.py    导入表情包
│  ├─ import_messages.py 导入群聊
│  ├─ build_vectors.py   建向量（--force 全量重建）
│  ├─ run_eval.py        检索评测（三种模式并排）
│  ├─ run_agent_eval.py  Agent 评测（50 条 + 6 条上下文题）
│  ├─ pick_queries.py    翻聊天记录，帮你想评测题
│  ├─ check_queries.py   评测集体检（JSONL 有没有写错）
│  ├─ rebuild_fts.py     只重建关键词索引（不需要原始库）
│  ├─ gen_report.py      生成周报（--week 2026-W46 / last / --dry）
│  ├─ bench_routing.py   路由实验：规则 / 小模型 / 大模型 / 混合 四方案对比
│  └─ db_maintenance.py  WAL 合并 / VACUUM
├─ web/
│  ├─ index.html         搜索页（原生三件套，无框架）
│  ├─ agent.html         Agent 页（带工具调用记录）
│  ├─ login.html         口令页
│  ├─ portfolio.html     作品集页（公开，不要口令）
│  ├─ coach.html         学习教练：选一周、生成、看事实清单
│  └─ admin.html        管理面板（轮询任务状态、看日志）
├─ docs/
│  ├─ deploy.md          部署 + 事故复盘 + SSH 三端口
│  ├─ agent.md           Agent 的设计与三个刻意限制
│  ├─ routing.md         路由实验：20 行正则凭什么和大模型打平
│  ├─ coach.md           周报为什么是「先算事实再让模型写」
│  └─ cloudflare.md      域名与 NS
├─ data/
│  ├─ raw/               ← AstrBot 数据库副本放这儿
│  └─ group_brain.db     运行时生成的索引
└─ eval/
   ├─ queries.jsonl      检索评测集（48 条）
   ├─ agent_cases.jsonl  Agent 评测集（50 条）
   ├─ agent_history_cases.jsonl 上下文题（6 条，带 history）
   ├─ agent_holdout.jsonl 留出集（10 条，写完规则之后才写的）
   └─ 怎么补评测集.md     给未来的自己看的操作手册
```

---

## 接口

```
GET /api/search?q=大肥鱼&kind=all&limit=10&mode=auto   # auto/keyword/vector/hybrid
GET /api/context?id=8&before=4&after=4                 # 记忆回放：前后几条
GET /api/stats
GET /api/meme/{id}                                     # 直接返回图片
GET /api/agent?q=谁说过要吃鱼片&history=[...]           # Agent 模式；history 是刚才的对话（JSON），用来解「刚才那句」
GET /portfolio                                         # 作品集页（唯一不要口令的页面）
GET /api/health                                        # 会真查一次库，回 db_ok
GET /login · POST /login · GET /logout                 # 口令门
GET /admin · GET /api/admin/status · POST /api/admin/run?task=fts
                                                       # 管理面板：fts/memes/vectors/wal/vacuum
GET /api/coach?week=2026-W46                           # 读周报（+最近生成过哪些周）
POST /api/coach?week=2026-W46                          # 生成/重算周报，要调模型、10~20 秒
```

---

## 管理面板（v0.7）

打开 `https://zkysever.com/admin`，五个按钮，都是「点一下 = 在服务器上跑一次脚本」：

| 按钮 | 跑什么 | 什么时候用 |
|---|---|---|
| 重建关键词索引 | `rebuild_fts.py` | 索引条数和消息条数对不上时（面板会标红） |
| 重导表情包 | `import_memes.py` | 图库加了新图 |
| 重建向量 | `build_vectors.py --force` | 换 embedding 模型、或向量条数不对。**会调接口、花钱** |
| 合并 WAL | `db_maintenance.py` | `-wal` 文件涨到几十 MB |
| VACUUM | `db_maintenance.py --vacuum` | 删过大量数据、想把库文件压回去 |

三条设计上的取舍：
1. **同一时间只跑一个任务**（全局锁）。两个 `build_vectors` 同时跑会互相覆盖向量表，还抢同一份 API 额度。
2. **没有「重新导入聊天记录」按钮**，因为服务器上根本没有 AstrBot 原始库——数据是从本机导入后传上去的。
   要更新聊天记录，只能在本机重跑 `import_messages.py`，再把 `data/group_brain.db` 传上去。
3. **日志会截断到 8000 字符**。不看日志的批处理按钮等于黑箱，看全部日志又没必要。

---

## 学习教练（A4）

`/coach`：选一周，点生成，拿到三段——这周做了什么 / 卡在哪 / 下周一件具体的事。

和 `/agent` 的区别是**谁主导**：Agent 是模型自己决定查什么；教练是**先把这周的事实算好**（GitHub 提交 + 群聊活跃天数，只做聚合、不取原文），模型只负责组织语言。

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/coach?week=2026-W46` | 读该周周报 + 最近生成过哪些周 |
| POST | `/api/coach?week=2026-W46` | 生成或重算（调一次 DeepSeek，10~20 秒） |

三个取舍：
1. **生成做成 POST**。它花钱又慢，做成 GET 的话刷新一次页面就重新扣一次钱。
2. **`facts` 列必须留档**。报告里的每句话都应该能在 facts 里找到出处，哪天真发现它在编，两列一比就知道。
3. **只给纯文本、不给 JSON**。模型对着 JSON 会照着念字段名（“commits 数组长度 19”），对着人话才写人话。

细节与已知短板见 `docs/coach.md`；命令行版 `python scripts/gen_report.py --week last`。

---

## 为什么是 bigram 不是 jieba

一句话：**索引和查询必须用同一套切法**。
jieba 切"大肥鱼"可能切成"大/肥鱼"，用户搜"肥鱼"就废了。
bigram 把"大肥鱼"变成 `大肥 肥鱼`，搜"肥鱼"必中。
代价是索引变大、bm25 打分略糙——122 张图 + 484 条消息，这点代价可以忽略。

单字查询（比如搜"鱼"）bigram 匹配不到，会自动退化成 `LIKE`。

---

## 跑评测

```
python scripts/run_eval.py
```

这套 48 条查询现在的成绩（`EVAL.md` 有完整对比表）：

| 模式 | Recall@5 | MRR | 平均延迟 |
|---|---|---|---|
| 纯关键词 | 0.938 | 0.899 | ~0ms |
| 纯向量 | 0.958 | 0.880 | 530ms |
| 关键词 + 向量（默认） | **1.000** | **0.939** | 520ms |

1.000 是「这套题的天花板」不是「满分」，说明题目还不够难；520ms 是因为每次查询都要联网换一次向量，
是真代价不是 bug。加查询时的原则不变：**别改评测集去迁就结果**——那等于自己骗自己。

---

## 数据从哪来

- **群聊**：`data/raw/data_v4.db`，表 `platform_message_history`。
  三件套 `.db` / `.db-wal` / `.db-shm` 必须一起拷，只拷主库会丢最近的写入。
- **表情包**：直接读 `%USERPROFILE%\.dsh\meme-packs\<pack>\index.db`，
  每张图的 caption 就是它的检索文本。图片本身不搬，库里存绝对路径。

本机跑时服务只听 `127.0.0.1`；线上由 cloudflared 隧道转进 `127.0.0.1:8000`，公网不直接暴露端口。

---

## 下一步

- ~~A3：本地小模型路由~~ 做了一半：四个方案比完了（`docs/routing.md`），但用的是免费云小模型不是本地 Ollama
  （GitHub 下载 0 字节/秒、镜像 147 KB/s，1.85 GB 要 3.5 小时，不值当）。本地部署留到有 GPU 空的时候再说
- ~~v0.7：Web 管理面板~~ 已完成。还差的见下面两条
- ~~**接口带上下文**：`/api/agent` 加 `history` 参数~~ —— 已做（A2 v0.2）：上下文题 0/6 → 6/6，见 `docs/agent.md`
- **规则前置**：`agent.run()` 前加一层 `rule_route()`，规则有把握就跳过「决定调不调」那一次模型往返。
  A3 的结论是这条路能省掉几乎全部模型调用，现在缺的是把它接进真实接口再跑一遍 50 条
- **工程收尾**：GitHub Actions 跑 `check_queries.py` + 关键词模式评测（不花钱）、`requirements.txt` 锁版本、
  补 LICENSE、每天把 `data/group_brain.db` 备份回本机
- **周报推送**：现在只能自己开网页看，推群/推微信还没做（`docs/coach.md` 第七节第 4 条）

写代码之前先写 README——这条规矩是你自己定的。
