# A6 · 群内词库：它从群里自己学说话

> 一句话：群里那些圈外人看不懂的说法（外号、梗、黑话），它自己上网查明白，记住，下次直接用。
> 学习发生在服务器上 —— 你的电脑关不关都不影响。

---

## 1. 要解决的是什么

A1 会检索、A2 会调工具，但它们懂的是「通用中文」。群里真正每天在用的词是另一套：

```
@147 你这波开盒有点狠
大肥鱼今天怎么不冒泡
把 @147 开个盒
```

「开盒」「大肥鱼」「147」这些，模型不知道是啥，`search_memory` 也搜不出来（群里根本没人解释过）。
旧版本只有一条路补：**人教**。而 `app/learn.py` 的纠错记忆记的是「这句话该这么回」，
它记不住「这个词是什么意思」。

所以 A6 补的是**词的层面**：一个会自己长大的小词典。

---

## 2. 两条学习线（为什么需要两条）

| 线 | 触发 | 优点 | 漏点 |
|---|---|---|---|
| **observe**（被动） | 它回完一句话之后，回头判「刚才哪几个词我其实没把握」 | 精准，学的都是真碰到的 | **没人问就永远学不到** |
| **mine**（主动） | 每天 04:40 翻前两天的群聊，挑「圈外人看不懂」的说法 | 主动补齐，不等被问 | 只从字面猜，可能挑到没用的词 |

只有被动那条会漏掉大量词（群里黑话不是被问到才出现的）；只有主动那条会学一堆没人用的词。
两条共用同一个词库、同一份额度，谁先学到算谁的。

---

## 3. 数据流

```
QQ 群消息
   │
   ├─ batch_reply（priority=-200，负责回话）
   │     └─ 回完 → POST /api/lexicon/observe  ──┐
   │                                             │
   └─ gb_lexicon（priority=-300，只记账不拦不回） │
         └─ 攒够 10 条 / 隔 5 分钟 → POST observe ┘
                                                  │
                                    group-brain（服务器）
                                                  │
                      判词：这几个词它真的不懂吗？
                                                  │
                      搜：{词} {原句} / {词} 什么意思 / {词} 是什么梗 / {词}
                                                  │
                      写：模型只依据材料写一句解释 + 置信度
                                                  │
                      存：lexicon 表（confidence ≥ 0.40 才进）
                                                  │
回复时：GET /api/lexicon/context?text=…  → 拼进 prompt（≤3 条，置信度 ≥0.45）
```

调 cron 那条（主动线）：

```
04:40  scripts/learn_terms.py  →  pick() 挑 1–8 个  →  learn() 逐个上网查  →  写库
```

---

## 4. 为什么放在云端

用户的原话是「做在云端，不要被我的电脑是否开机限制」。这条约束直接决定了三个设计：

1. **判词和搜索都在 `group-brain` 里**，不在插件里 —— 插件只做「攒消息 + 转发」，因为它跑在 AstrBot 容器里，容器重启就没了状态，不该承担学习。
2. **插件走隧道域名**（`https://zkysever.com`）而不是 `172.17.0.1:8000`：`group-brain` 的 `ExecStart` 是 `--host 127.0.0.1 --port 8000`，容器里直接连内网 IP 是不通的（实测 `curl http://172.17.0.1:8000/api/health` 空，`curl https://zkysever.com/api/health` = 200）。
3. **token 落在服务器文件里**（`/opt/qqbot/astrbot-data/gb_lexicon_token.txt`），不写进配置 —— 换机器不丢，人也能一眼看到。

---

## 5. 表结构

```sql
CREATE TABLE lexicon (
  term        TEXT PRIMARY KEY,   -- 词本身（清洗过标点，≤12 字）
  gloss       TEXT NOT NULL,      -- 一句话解释（≤120 字）
  kind        TEXT,               -- 梗 / 外号 / 术语
  source      TEXT,               -- web / group / 人
  urls        TEXT,               -- 参考来源（JSON 数组，便于复查）
  confidence  REAL,               -- 0–1，<0.40 不入库，<0.45 不给模型用
  hits        INTEGER,            -- 被用了几次（判断有没有用）
  asked       INTEGER,            -- 学了几次（≥2 就放弃，别反复烧钱）
  status      TEXT,               -- auto / guess / confirmed / rejected
  first_seen  TEXT,
  updated     TEXT
);

CREATE TABLE lexicon_jobs (       -- 每次学的结果，出问题靠它复盘
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, term TEXT, ok INTEGER, note TEXT
);
```

`lexicon_jobs` 只留最近 2000 条 —— 判词日志会一直长，不设上限迟早拖慢写入。

---

## 6. 三道闸门（为什么不是「搜到什么就信什么」）

1. **判词闸门**：不是所有没见过的词都要学。`_JUDGE_SYSTEM` 要求模型只挑「我确实不确定」的，并且给出原句片段（`quote`）。模型说「我知道」就不学 —— 大部分生词它其实是懂的。
2. **检索闸门**：查询分四档（带原句 → 什么意思 → 是什么梗 → 裸词），前档没结果才降档。带原句是为了纠偏：光搜「香香软软的小猫娘」会命中美妆页面，混进「他是一只…」这类上下文才能拉回群里的意思。
3. **置信闸门**：模型必须只依据网页摘要写解释，并自评置信度。`< 0.40` 不入库（记一条失败 job）；`< 0.45` 存着但不给模型用（留着给人看）。实测这条能挡住「搜到不相干页面还硬编」的情况。
4. **兜底猜测（status=`guess`）**：网页上根本没有词条的词（群内外号、自造说法）走这条 —— 只凭原句猜一句，置信度封顶 **0.35**，永远低于 `CONF_USE`，所以**不会**被拿去用；等人在 `/api/lexicon/confirm` 点头后才变 `confirmed`（0.9）生效。

为什么第四条是这么设计的：群里天天在用的词（「大肥鱼」「鱼鱼」）在网上是不存在的，只走「先搜后写」等于永远学不会它们；但猜出来的东西直接拿去聊天，叫错外号比不认识更难堪。所以「猜」只负责**进待审**，不负责生效。

---

## 7. 额度（按钱定的，不是按技术定的）

| 常量 | 默认 | 位置 | 作用 |
|---|---|---|---|
| `LEX_MAX_TERMS_DAY` | 40 | `app/config.py` | 一天最多学几个词 |
| `LEX_MAX_LOOKUPS_DAY` | 12 | `app/config.py` | 一天最多联网查几次（更贵） |
| `MAX_ATTEMPTS` | 2 | `app/lexicon.py` | 同一个词最多试 2 次 |
| `daily_cap` | 120 | 插件配置 | 一天最多上报几批（防刷） |
| `min_lines` / `interval_seconds` | 10 / 300 | 插件配置 | 攒够 10 条、隔 5 分钟才上报 |
| `quiet_hours` | `1-8` | 插件配置 | 凌晨不打扰（也不学习） |

一次学习 ≈ 1 次搜索 + 1 次小模型调用，成本可以忽略；额度是为了防止「某个词被反复学」这种失控。

---

## 8. 怎么用

### 群里（不用管）

被 @ 时它回话，词库自动生效；回完自动上报。它学会的词会出现在回复里，不用人提醒。

### 手动补一轮

```bash
cd /opt/group-brain
set -a; . ./.env; set +a
.venv/bin/python scripts/learn_terms.py --dry            # 只看它想学什么，不写库
.venv/bin/python scripts/learn_terms.py --limit 3        # 真学 3 个
.venv/bin/python scripts/learn_terms.py --days 7         # 回看 7 天
```

### 看词库 / 纠正

```bash
# 人看（走网页口令，不是 token）
curl -s -c /tmp/ck.txt -X POST -d 'pw=<口令>' https://zkysever.com/login
curl -s -b /tmp/ck.txt https://zkysever.com/api/lexicon/list | python -m json.tool

# 发现解释错了：直接改成对的
curl -s -X POST https://zkysever.com/api/lexicon/correct \
  -H 'x-gb-token: <LEARN_TOKEN>' -H 'content-type: application/json' \
  -d '{"term":"开盒","gloss":"群里指扒人隐私"}'

# 这个词别学了（错的、或者不该记的）
curl -s -X POST https://zkysever.com/api/lexicon/reject \
  -H 'x-gb-token: <LEARN_TOKEN>' -H 'content-type: application/json' \
  -d '{"term":"杯子"}'

# 猜出来的词（status=guess，不会自动用）觉得对，点头放行；顺手能改解释
curl -s -X POST https://zkysever.com/api/lexicon/confirm \
  -H 'x-gb-token: <LEARN_TOKEN>' -H 'content-type: application/json' \
  -d '{"term":"鱼鱼","gloss":"群友对大肥鱼的叫法"}'
```

### A2 里也能主动查

`/agent` 多了一个工具 `lookup_lexicon(term, context)`：库里没有就顺手上网查一次并记住。
命令行版（`app/agent.py` 的 `run()`）用的是另一条路：每轮问答前把命中的词拼成一行提示塞进 system，
因为「它本来就不知道自己不知道」，指望它主动调工具是不现实的。

**但这个工具默认是关的（`LEXICON_TOOL=0`），因为实测它有害**（2026-11-19，同一份线上代码、同一批 50 条）：

| 配置 | 正确率 | 误触发 | 平均轮数 |
|---|---|---|---|
| `LEXICON_TOOL=0`（默认） | **48/50 = 0.960** | **0/12** | 0.94 |
| `LEXICON_TOOL=1` | 43/50 = 0.860 | 2/12 | 1.12 |

失败样例全是「多查一下」：`你的设定是什么` 去调 `search_facts`、`上次那个说自己是鱼的人` 多调一次、
`昨天有谁在群里说话` 转到第 4 次。**知识以「提示」形式注入无害，做成「工具」就有毒**——
工具越多，模型越爱先查一遍，而这些问题本来就该直接答。开关留着，随时能复现这个结论。

### 网页：`/lexicon` 审阅页（v0.22.2 加的）

上面那几个 curl 是给插件和脚本用的。**人是不会天天敲 curl 的**，而 A6 的核心规矩是
「猜出来的词不许自己生效，必须人点头」——没有页面，这条规矩就等于没落地：
`guess` 会一直躺在库里等人，等到最后还是没人管。

所以加了一个页面（`web/lexicon.html`，入口在管理面板头部「群内词库」）：

| 按钮 | 调用 | 效果 |
|---|---|---|
| **点头** | `POST /api/lexicon/review {action:"confirm"}` → `lexicon.confirm()` | `guess` → `confirmed`，置信度 0.9，**下一句回复就能用** |
| **改解释** | `{action:"gloss", gloss:"…"}` → `lexicon.correct()` | 改成你写的，`confirmed` + 置信度 1.0（人写的比模型可信） |
| **驳回** | `{action:"reject"}` → `lexicon.reject()` | `rejected`，`glosses_for()` 从此不再返回它 |

页面本身分五档：全部 / 待人点头 / 网上查到 / 已确认 / 已驳回，排序是
`guess → auto → confirmed → rejected`，同档按置信度降序 —— **这一页的唯一任务是「把 guess 清干净」**，
所以待审的必须排最前。

**权限口径**：这三个接口和页面都**不在 `PUBLIC` 里**，靠的是网页口令门（`gb_gate` cookie），
不是 `x-gb-token`。理由是它们做的是「人做决定」，而 token 是给机器用的；
把 token 贴在浏览器里等于把机器凭据发到前端。插件那三个接口（context / observe / correct）
反过来一律要 token，因为它们是自动跑的。

---

## 9. 实测记录（2026-11-19）

本地库（483 条，只到 2026-11-05）跑 `--dry`：挑到 `大肥鱼 / 这条鱼 / 鱼鱼 / 开盒`，
原句分别是「@大肥鱼 你能修改你自己的设定吗」「我又更新了一下这条鱼」「鱼鱼先冒个泡」「把 @147 开个盒」。

真学一轮（`--limit 5`）：

```
[挖词] 挑到 5 个：大肥鱼、这条鱼、开盒、香香软软的小猫娘、杯子
[学会] 1 个：香香软软的小猫娘
  跳过 大肥鱼（已经会了）
  跳过 这条鱼（没学会）
  跳过 开盒（已经会了）
  跳过 杯子（没学会）
[词库] 2 → 3 条
```

学到的两条（人工看是对的）：

| 词 | 解释 | 置信 |
|---|---|---|
| 大肥鱼 | DeepSeek 鲸鱼 Logo 被识图误认成「蓝色大肥鱼」，后成其昵称 | 0.90 |
| 开盒 | 网络黑话，指非法搜集并公开他人隐私信息，属网络暴力 | 0.95 |

`这条鱼` / `杯子` 没学会是**预期行为**：前者搜不到独立材料，后者置信度不够 —— 宁可空着，也别写错。

第二轮（服务器，658 条真实群聊）验证了「猜」这条兜底：

```
[挖词] 挑到 4 个：鱼鱼、大肥鱼、鱼受宠若惊、自己陪自己
[学会] 0 个 → 全部记成「模型说不确定」→ 退化成 guess（confidence 0.30–0.35，status=guess）
```

本地实测 `lexicon.lookup(conn, '鱼鱼', '鱼鱼又来自己陪自己了')` → `群里对某位成员的称呼` / `guess` / `0.30`；
`glosses_for('鱼鱼今天在吗')` 返回 `[]`（不给用）；`lexicon.confirm(conn, '鱼鱼')` 之后返回 `['鱼鱼']`。
这就是设计意图：**猜可以入库，但不能自己生效。**

---

## 10. 已知短板（别指望它超出这些）

1. **只认字面**：「大肥鱼」和「鱼鱼」是同一个东西，但它当成两个词存。同义词合并需要人工或聚类，这版没做。
2. **一句解释装不下长梗**：`gloss` 上限 120 字，剧情梗（比如某个连续剧的整活）只能记个大概。
3. **上限由搜索质量决定**：搜不到干净材料就学不会，这是外部依赖，不是模型不行。
4. **不会自动退场**：错的解释会一直躺在库里（用到时置信度低，影响有限），要人 `reject` 或者 `correct`。
5. **额度是钱定的**：40 词/天对现在的群够用，群再热闹要往上调 `LEX_MAX_TERMS_DAY`。
6. **不学人**：它只学「词」，不学「谁是谁的朋友」「谁在开玩笑」。那是 `facts` 那条线的事。
