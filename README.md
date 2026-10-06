# 盲样分类复核 API（FastAPI + SQLite）

在盲样复核流程中，建立批次时登记**允许类别**和每个样本代号的**真实类别**；系统只在
创建响应中一次性发放三份凭据（评审 1、评审 2、仲裁者）。两名评审各自独立、盲态地
完成覆盖全部样本的分类并交卷；交卷后不可修改。双方交卷齐全后，仲裁者只能看到**有
分歧的样本及双方选项**，逐项从中二选一；一致样本直接取共同分类。分歧全部裁定后批次
一次性**封存揭盲**；若本就没有分歧，第二份交卷到达时立即封存。封存后公开查询返回每
个样本的最终分类、真值以及两名评审的各自正确数。

## 一条命令启动

```bash
docker compose up --build
```

- 本地端口：**http://localhost:8000**
- 交互式文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：`GET http://localhost:8000/health`
- SQLite 数据保存在命名卷 `blind_review_data`（容器内 `/data/blindreview.db`），
  容器重启 / 重建后数据仍在。

需要停掉并清空数据时：

```bash
docker compose down            # 停止，数据保留
docker compose down -v         # 停止并删除数据卷
```

---

## 业务阶段（stage）

| 阶段 | 含义 |
| --- | --- |
| `accepting_submissions` | 等待评审交卷（批次创建后的初始阶段） |
| `awaiting_arbitration` | 两份交卷齐全且存在分歧，等待仲裁 |
| `sealed` | 已封存揭盲，公开结果可读，一切写入关闭 |

流转：

```
accepting_submissions ──第二份交卷、有分歧──▶ awaiting_arbitration ──裁定最后一项分歧──▶ sealed
        │
        └──────────────第二份交卷、无分歧──────────────────────────────────────────────▶ sealed
```

## 凭据与访问控制

- 三份凭据为不透明随机 token（`secrets.token_urlsafe(32)`），数据库只存 SHA-256
  哈希；明文**只在创建响应中出现一次**，服务端无法再次取回。
- 所有评审 / 仲裁接口都要求请求头 `Authorization: Bearer <token>`。
- 凭据与批次绑定：A 批次的 token 不能用于 B 批次。
- 角色隔离：
  - 评审只能 `GET/POST` 自己的 `/submission`，且 `GET` 只返回**自己的**提交状态；
  - 仲裁接口只有仲裁 token 可用；
  - 封存前 `GET /batches/{id}` 无需凭据，但只返回样本代号与阶段，不泄露真值、
    双方分类或提交进度。

## HTTP 调用示例

设 API 地址为 `http://localhost:8000`。下面的命令可直接顺序执行；也可以直接运行
`bash examples/demo.sh`（需要 `curl` 与 `python3`）。

### 1. 建立批次，拿到三份凭据（唯一一次返回明文 token）

```bash
curl -sS -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "categories": ["cat", "dog"],
        "samples": [
          {"code": "S01", "true_label": "cat"},
          {"code": "S02", "true_label": "dog"},
          {"code": "S03", "true_label": "cat"}
        ]
      }'
```

响应（请妥善保存三个 token）：

```json
{
  "batch_id": "1f2c...",
  "stage": "accepting_submissions",
  "reviewer1_token": "....",
  "reviewer2_token": "....",
  "arbiter_token": "....",
  "sample_count": 3
}
```

约束：类别至少 2 个且不重复；样本代号不重复；标签非空。否则返回 `422`。

### 2. 封存前的公开查询：只有代号和阶段

```bash
curl -sS http://localhost:8000/batches/<batch_id>
```

```json
{"batch_id": "1f2c...", "stage": "accepting_submissions",
 "sample_codes": ["S01", "S02", "S03"],
 "samples": null, "reviewer1_correct": null, "reviewer2_correct": null,
 "sample_count": 3}
```

### 3. 评审查看自己的提交状态

```bash
curl -sS http://localhost:8000/batches/<batch_id>/submission \
  -H "Authorization: Bearer <reviewer1_token>"
```

未交卷时 `submitted=false`、`classifications=null`；交卷后只返回**自己的**分类。

### 4. 两名评审各交一次卷（必须覆盖全部样本，标签必须在允许类别内）

```bash
curl -sS -X POST http://localhost:8000/batches/<batch_id>/submission \
  -H "Authorization: Bearer <reviewer1_token>" \
  -H 'Content-Type: application/json' \
  -d '{"classifications": {"S01": "cat", "S02": "dog", "S03": "dog"}}'

curl -sS -X POST http://localhost:8000/batches/<batch_id>/submission \
  -H "Authorization: Bearer <reviewer2_token>" \
  -H 'Content-Type: application/json' \
  -d '{"classifications": {"S01": "cat", "S02": "dog", "S03": "cat"}}'
```

第二份交卷存在分歧时返回 `"stage":"awaiting_arbitration"`；若全部一致则直接
`"stage":"sealed","sealed_now":true`。

规则：
- 覆盖不全 / 多给代号 / 标签不在允许集合内 → `422`，整次交卷被拒绝，不落任何数据；
- 已交卷后再次提交 → `409`，原判断绝不被改写；
- 阶段已离开 `accepting_submissions` 后提交 → `409`。

### 5. 仲裁者查看分歧样本（看不到真值，也看不到一致样本）

```bash
curl -sS http://localhost:8000/batches/<batch_id>/disputes \
  -H "Authorization: Bearer <arbiter_token>"
```

```json
{
  "batch_id": "1f2c...",
  "stage": "awaiting_arbitration",
  "remaining": 1,
  "disputes": [
    {"code": "S03", "reviewer1_label": "dog",
     "reviewer2_label": "cat", "chosen_label": null}
  ]
}
```

### 6. 逐项裁定（最终分类只能是双方标签之一）

```bash
curl -sS -X POST http://localhost:8000/batches/<batch_id>/adjudications \
  -H "Authorization: Bearer <arbiter_token>" \
  -H 'Content-Type: application/json' \
  -d '{"code": "S03", "label": "cat"}'
```

- 一致样本不能裁定 → `422`；
- `label` 不是该样本双方给出的两个标签之一 → `422`；
- 同一项重复裁定 → `409`，第一次的选择保留不变；
- 裁定最后一项分歧时响应 `"sealed_now": true, "stage": "sealed"`。

### 7. 封存后揭盲查询

```bash
curl -sS http://localhost:8000/batches/<batch_id>
```

```json
{
  "batch_id": "1f2c...",
  "stage": "sealed",
  "sample_codes": ["S01", "S02", "S03"],
  "samples": [
    {"code": "S01", "final_label": "cat", "true_label": "cat",
     "reviewer1_label": "cat", "reviewer2_label": "cat",
     "correct": true, "source": "agreement"},
    {"code": "S02", "final_label": "dog", "true_label": "dog",
     "reviewer1_label": "dog", "reviewer2_label": "dog",
     "correct": true, "source": "agreement"},
    {"code": "S03", "final_label": "cat", "true_label": "cat",
     "reviewer1_label": "dog", "reviewer2_label": "cat",
     "correct": true, "source": "adjudication"}
  ],
  "reviewer1_correct": 2,
  "reviewer2_correct": 3,
  "sample_count": 3
}
```

`source` 标明最终分类来源：`agreement`（双方一致直接采用）或
`adjudication`（仲裁裁定）。`reviewer*_correct` 按各评审自己的分类与真值
逐样本比较统计。

## 状态码约定

| 码 | 含义 |
| --- | --- |
| 200 / 201 | 成功（写入类接口为 201） |
| 401 | 缺少 `Authorization: Bearer ...` |
| 403 | token 无效 / 不属于该批次 / 角色不匹配 |
| 404 | 批次或样本不存在 |
| 409 | 状态冲突：已交卷、已裁定、阶段已关闭等（幂等拒绝，不改数据） |
| 422 | 请求体校验失败（覆盖不全、类别越界、不能裁定一致样本等） |
| 503 | SQLite 锁竞争超时，可安全重试 |

## 一致性与安全保证（设计要点）

- **唯一揭盲点**：阶段翻转为 `sealed` 是公开真值的唯一开关；翻转之前所有公开
  投影都只含代号 + 阶段，仲裁查询的 SQL 根本不选取 `true_label` 列。
- **写串行化**：每个写操作都在 `BEGIN IMMEDIATE` 事务内执行，SQLite 在事务
  开始即取写锁，配合 `busy_timeout=5000`、WAL 日志。交卷、裁定、封存互相
  串行，交错请求不可能提前揭盲或产生两份不同结果。
- **不可改写**：`submissions` 主键 `(batch_id, reviewer)`、`adjudications`
  主键 `(batch_id, code)` 从数据库层保证一人只交一卷、一分歧只裁定一次；
  并发重复请求最多一个成功。
- **封存原子性**：封存前再次核对“两份交卷齐全且分歧全部有裁定”，最后一项
  裁定与阶段翻转在同一事务内提交；无分歧时第二份交卷与封存同事务提交。
- **结果单一来源**：最终分类和正确数在读取时由不可变的交卷 / 裁定行确定性
  派生（一致取共同值，分歧取裁定值），不另存易产生不一致的冗余副本。
- **持久化**：数据落盘在挂载卷上，`synchronous=NORMAL` + WAL 在崩溃时仍保证
  已提交事务不丢；重启后阶段、交卷、裁定与封存结果原样保留，写入限制继续生效。

## 目录结构

```
app/
  db.py        # SQLite 连接、WAL/外键 PRAGMA、建表、写事务上下文
  models.py    # Pydantic 请求 / 响应模型与输入校验
  security.py  # token 生成、SHA-256 哈希、恒定时间比较
  service.py   # 封存（唯一揭盲点）
  main.py      # FastAPI 路由：批次、公开查询、交卷、分歧、裁定
examples/demo.sh   # 可直接执行的完整流程演示
tests/e2e_test.py  # 真实 uvicorn 上的端到端测试（含并发竞态与重启持久化）
Dockerfile
docker-compose.yml
requirements.txt
```

## 不用 Docker 的本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./blindreview.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行端到端测试（另需 `pip install httpx`）：

```bash
python tests/e2e_test.py
```

测试覆盖：盲态查询、角色隔离与跨批次拒绝、覆盖 / 类别校验、交卷不可改写、
仲裁只能在双方交卷后查看分歧、裁定选项约束与不可重复、一致即封存 / 全分歧裁定
后封存、封存结果与正确数统计、**8 路并发重复交卷仅一份成功**、
**8 路并发裁定最后一项仅一份成功且结果唯一**、以及**重启后状态与不可变性保持**。
