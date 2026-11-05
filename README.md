# group-brain

群聊记忆体 · v0.1

把 AstrBot 里攒下来的群聊记录 + 两套表情包，变成**一条能搜的本地索引**。
不带任何大模型调用，不用联网，双击就能跑。

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
| 群聊消息关键词检索（483 条） | ✅ |
| 表情包按描述/关键词检索（122 张） | ✅ |
| 检索结果直接出图 | ✅ |
| 中文 bigram 分词 | ✅ |
| 评测跑分（Recall@5 / MRR） | ✅ 基线 |
| 向量检索 + RRF | ⬜ v0.4 |
| 部署到 zkysever.com | ⬜ v0.5 |
| Web 管理面板 | ⬜ v0.6 |

---

## 目录结构

```
group-brain/
├─ 双击启动.bat          ← 唯一需要双击的东西
├─ app/
│  ├─ config.py          路径 / 端口 / 可选 key
│  ├─ text.py            中文 bigram 分词（整个项目的检索地基）
│  ├─ db.py              建表 + 连接
│  ├─ search.py          FTS5 检索 + RRF 融合
│  ├─ embeddings.py      向量接口（v0.4 才启用）
│  └─ main.py            FastAPI：网页 + 3 个接口
├─ scripts/
│  ├─ init_db.py         建表
│  ├─ import_memes.py    导入表情包
│  ├─ import_messages.py 导入群聊
│  └─ run_eval.py        跑评测
├─ web/index.html        前端（原生三件套，无框架）
├─ data/
│  ├─ raw/               ← AstrBot 数据库副本放这儿
│  └─ group_brain.db     运行时生成的索引
└─ eval/queries.jsonl    评测集（现在 10 条，目标 50 条）
```

---

## 三个接口

```
GET /api/search?q=大肥鱼&kind=all&limit=10
GET /api/stats
GET /api/meme/{id}          # 直接返回图片
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

所有数据都在你本机，服务只听 `127.0.0.1`。

---

## 下一步（v0.2）

- 加 `/api/context`：给一条消息，返回它前后 5 条，做"记忆回放"
- 加搜索历史（localStorage）
- 评测集从 10 条补到 20 条

写代码之前先写 README——这条规矩是你自己定的。
