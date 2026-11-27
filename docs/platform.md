# 三线并进工作表（A7 平台化）

> 目标：把已有的 group-brain（A1/A2/A6）、B 站自动化链（发布/推荐/抽象日报/鲸鱼）、评测体系，
> 收成一套**能对外验证**的产品：开放 API、运营中台、群助手训练台。
> 全部跑在 `zkysever.com` + 阿里云 118.178.109.54（2C1.7G）+ Cloudflare 隧道上。

---

## 0. 三条线是什么关系

```
        ┌──────────── 共享地基（第 1 周，只做一次）────────────┐
        │  租户/API Key/配额  ·  任务模型(job/run/step)  ·  /metrics  │
        └───────┬──────────────────┬──────────────────┬──────────┘
                │                  │                  │
        ①开放 API 产品       ②运营中台          ③群助手训练台
        api.zkysever.com     console.zkysever.com   train.zkysever.com
        检索/记忆/词库         编排+日志+告警        上传→词库→接入
```

- ① 复用 `app/search.py`、`app/memory.py`、`app/lexicon.py`（≈90% 代码不动）
- ② 复用 `scripts/prep_next.py`、`publish_next.py`、`tools/publish_bsk.py`、`app/curator.py`、`app/weird.py`（100% 业务逻辑不动，只包一层调度）
- ③ 复用 `app/lexicon.py`、`web/lexicon.html`、`eval/*.jsonl`（100%）

**顺序**：地基 → ① → ② → ③。理由：①最快出可点开的成果；②把 12 条 cron 收进来之后，③ 的定时分析直接复用调度器。

---

## 1. 六周工作表

| 周 | 主线 | 交付物（可验收） | 验收指标 |
|---|---|---|---|
| W1 | 地基 | `api_keys`/`usage` 表 + 鉴权中间件；`jobs`/`runs` 表 + 调度器骨架；`/metrics` | 无 Key 一律 401；`curl /metrics` 有指标 |
| W2 | ① API | `api.zkysever.com/docs`（OpenAPI）、`/v1/search`、`/v1/ask`、`/v1/lexicon`；控制台首页（调用量/延迟） | P95 < 300ms；文档可试跑 |
| W3 | ① SDK+bench | npm 包 `@zkysever/groupbrain`；curl/JS/Python 示例；100 条 benchmark 仓库 | `npm i` 后 3 行代码能查 |
| W4 | ② 中台 | 12 条 cron 迁入；任务列表/手动触发/实时日志 UI；失败重试+死信 | 杀进程不丢任务；成功率 ≥99% |
| W5 | ② 可观测 | Prometheus 指标 + Grafana 面板 + QQ 告警；自动周报 | 告警 < 2 分钟；周报自动生成 |
| W6 | ③ 训练台 + 收口 | 上传聊天记录 → 分析报告（活跃度/黑话 Top20/画像）→ 词库包 → 一键接入配置 | 上传到出报告 < 3 分钟；接入前后正确率对比 |
| W7+ | 打磨 | 作品集页放三张卡片（三个入口 + 数据）、技术长文 2 篇、简历三行 | Lighthouse ≥95、文章可读 |

> 每周五固定：部署到线上 + 截图归档到 `out/shots/<周>/` + 更新 `docs/platform.md` 的进度表。

---

## 2. 每天怎么协作（工作流）

1. **早上**：我看一遍昨晚的 cron 日志与线上 health，发一条「今天的 3 件事」。
2. **白天**：我做；每个功能走 `本地跑通 → scp 部署 → 验证脚本 → 截图 → commit + push`。
   改代码只用 `edit`/`write` 工具，远端命令一律写成 `.sh` 再 scp（这条是被坑出来的规矩）。
3. **晚上**：给你一段「今天完成 / 卡在哪 / 明天做什么 / 需要你拍板的一件事」。
4. **你只需要在三种时刻出现**：视觉/文案拍板、需要你账号的操作（扫码之外我已能用 cookie）、给方向取舍。

---

## 3. 数据模型（一次设计，三条线共用）

```sql
-- ① 租户与配额
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,            -- t_xxxx
  name TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,            -- k_xxxx
  tenant_id TEXT NOT NULL,
  prefix TEXT NOT NULL,           -- sk_live_abcd（展示用）
  hash TEXT NOT NULL,             -- sha256(完整 key)
  quota_day INTEGER NOT NULL DEFAULT 200,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, tenant_id TEXT NOT NULL, key_id TEXT,
  route TEXT NOT NULL, status INTEGER NOT NULL,
  ms INTEGER NOT NULL, tokens INTEGER NOT NULL DEFAULT 0
);

-- ② 任务编排
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, cron TEXT,
  script TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  timeout INTEGER NOT NULL DEFAULT 600, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
  started TEXT NOT NULL, ended TEXT, status TEXT NOT NULL,  -- running/ok/fail/dead
  attempt INTEGER NOT NULL DEFAULT 1, exit_code INTEGER,
  log_path TEXT, note TEXT
);

-- ③ 训练台（群空间）
CREATE TABLE IF NOT EXISTS spaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT,
  created_at TEXT NOT NULL, source TEXT  -- upload/qq/…
);
```

---

## 4. 每周的技术检查点

| 周 | 检查点 |
|---|---|
| W1 | `curl -s -o /dev/null -w '%{http_code}' https://zkysever.com/api/v1/search` → 401；`/metrics` 出 `gb_requests_total` |
| W2 | 文档页能「Try it」拿到结果；Dashboard 显示真实调用 |
| W3 | `npm view @zkysever/groupbrain version`；`npx` 示例跑通 |
| W4 | 把 `prep_next.py` 从 cron 摘掉，改由中台触发，第二天 13:00 正常发片 |
| W5 | 手动杀掉中台进程，任务在 60 秒内被重试恢复 |
| W6 | 上传一份 500 条聊天记录，3 分钟内出报告 |

---

## 5. 简历写法（最终三行）

- 「独立设计并交付中文群聊检索开放 API：多租户 Key、配额、OpenAPI 与 npm SDK，2C1.7G 服务器上 P95 ___ms；公开 100 条检索 benchmark。」
- 「把 8 个脚本 / 12 条 cron 重构为任务编排中台（重试·幂等·死信·Prometheus+Grafana+告警），支撑 13 期视频与每日推荐全自动运营。」
- 「做群聊知识蒸馏工具：上传记录即生成黑话词库与 FAQ，接入后群内问答正确率 __% → __%（50 条评测集）。」

---

## 6. 进度表（每周五更新）

| 周 | 状态 | 备注 |
|---|---|---|
| W1 | 进行中 | 地基：`app/tenant.py` 已建（租户/Key/用量/任务四张表 + 中间件） |
