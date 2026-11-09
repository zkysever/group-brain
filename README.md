# group-brain

群聊记忆体 · v0.6

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
| Web 管理面板（入库/重建索引） | ⬜ v0.7 |

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
│  └─ main.py            FastAPI：页面 + 接口
├─ scripts/
│  ├─ init_db.py         建表
│  ├─ import_memes.py    导入表情包
│  ├─ import_messages.py 导入群聊
│  ├─ build_vectors.py   建向量（--force 全量重建）
│  ├─ run_eval.py        检索评测（三种模式并排）
│  ├─ run_agent_eval.py  Agent 评测（30 条）
│  ├─ pick_queries.py    翻聊天记录，帮你想评测题
│  └─ check_queries.py   评测集体检（JSONL 有没有写错）
├─ web/
│  ├─ index.html         搜索页（原生三件套，无框架）
│  ├─ agent.html         Agent 页（带工具调用记录）
│  └─ login.html         口令页
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
```

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

第一次跑出来的数字（我用 node:sqlite 复刻同一套 SQL 先验过）：
**Recall@5 = 0.938，MRR = 0.823**（16 条查询，命中 15 条）。

唯一的 miss 是「生气的时候摆手」——关键词里没有「摆手」，这种**换个说法**的查询
就是向量检索要救的场子。你自己往 `eval/queries.jsonl` 里加查询时，
故意加几条"意思一样但字不一样"的，基线会掉下来，那是好事，掉得越多 v0.4 越有东西可讲。
**别改评测集去迁就结果**——那等于自己骗自己。

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
- **v0.7**：Web 管理面板（这大概是唯一还想加的功能，因为现在重新入库还得登服务器敲命令）
- **评测加难**：现在 30 条 Agent case 已经 30/30 饱和，要加「不出现原词」「指代」「聚合」三类

写代码之前先写 README——这条规矩是你自己定的。
