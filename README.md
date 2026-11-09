# group-brain

群聊记忆体 · v0.7

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
| Agent 评测（30 条，正确率 30/30，误触发 0/6） | ✅ `EVAL.md` 第七节 |
| 口令门（HMAC cookie + 限速，不走邮箱） | ✅ v0.4 |
| 部署：cloudflared 隧道 + systemd + 三端口 SSH | ✅ `docs/deploy.md` |
| Web 管理面板（重建索引 / 重导表情包 / 建向量 / 合并 WAL） | ✅ v0.7 |

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
│  ├─ run_agent_eval.py  Agent 评测（30 条）
│  ├─ pick_queries.py    翻聊天记录，帮你想评测题
│  ├─ check_queries.py   评测集体检（JSONL 有没有写错）
│  ├─ rebuild_fts.py     只重建关键词索引（不需要原始库）
│  └─ db_maintenance.py  WAL 合并 / VACUUM
├─ web/
│  ├─ index.html         搜索页（原生三件套，无框架）
│  ├─ agent.html         Agent 页（带工具调用记录）
│  ├─ login.html         口令页
│  └─ admin.html        管理面板（轮询任务状态、看日志）
├─ docs/
│  ├─ deploy.md          部署 + 事故复盘 + SSH 三端口
│  ├─ agent.md           Agent 的设计与三个刻意限制
│  └─ cloudflare.md      域名与 NS
├─ data/
│  ├─ raw/               ← AstrBot 数据库副本放这儿
│  └─ group_brain.db     运行时生成的索引
└─ eval/
   ├─ queries.jsonl      检索评测集（48 条）
   ├─ agent_cases.jsonl  Agent 评测集（30 条）
   └─ 怎么补评测集.md     给未来的自己看的操作手册
```

---

## 接口

```
GET /api/search?q=大肥鱼&kind=all&limit=10&mode=auto   # auto/keyword/vector/hybrid
GET /api/context?id=8&before=4&after=4                 # 记忆回放：前后几条
GET /api/stats
GET /api/meme/{id}                                     # 直接返回图片
GET /api/agent?q=谁说过要吃鱼片                         # Agent 模式，返回答复 + 工具调用记录
GET /api/health                                        # 会真查一次库，回 db_ok
GET /login · POST /login · GET /logout                 # 口令门
GET /admin · GET /api/admin/status · POST /api/admin/run?task=fts
                                                       # 管理面板：fts/memes/vectors/wal/vacuum
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

- **A3**：本地小模型路由（Ollama + qwen2.5:0.5b），100 条真实群消息 × 4 方案，比准确率 / 延迟 / 成本
- ~~v0.7：Web 管理面板~~ 已完成。还差的见下面两条
- **评测加难**：现在 30 条 Agent case 已经 30/30 饱和，要加「不出现原词」「指代」「聚合」三类

写代码之前先写 README——这条规矩是你自己定的。
