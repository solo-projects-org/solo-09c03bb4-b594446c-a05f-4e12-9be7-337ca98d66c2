# 铅含量双实验室比对系统

食品检验场景：甲、乙两个实验室对同批样品上报铅含量，报表列名与单位不同。
本系统统一两份 CSV 的列映射与单位换算，按规则比对并交付带**原始行号**的统一结果。

## 业务规则

1. 每份 CSV 最多 **500 个数据行**；分别为甲(A)、乙(B)配置：样品号列、测值列、换算为 mg/kg 的系数（换算值 = 测值 × 系数）。
2. **同一文件内样品号重复 → 整份拒收**（HTTP 400，给出两处行号），不产生新版本。
3. 仅当两家均上报、十进制换算后的差值 **≤ 0.05 mg/kg** 时才纳入：
   - 写入均值（两换算值的算术平均）、甲乙两侧换算值与**原文件物理行号**；
   - 仅一家上报、或差值 > 0.05 的样品列入拒收/缺报明细并在网页展示原因。
4. 所有数值用 `Decimal` 精确运算，避免浮点误差影响临界判定。

## 版本与并发控制（乐观锁）

- 每次提交（新文件）或重算（仅改映射）都生成**不可变版本快照**，并在一个 `BEGIN IMMEDIATE`
  事务内整体切换 `current_version`；失败时回滚，当前可用版本不受影响。
- 提交必须携带“所见版本号” `expected_version`：与当前版本不一致时返回 **409 冲突**，
  旧版本原样保留，网页提示后自动刷新到最新版本。
- 原始文件、映射、结果、拒收明细均按版本独立存储；下载可指定 `?version=N`，
  **只返回该版本快照**，并发修改/下载不会跨版本混用。
- 仅改映射的重算从数据库中的两份原始文件重新解析计算，无需重新上传。

## 启动（Docker Compose）

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `lead_data`（容器内 `/data/app.db`），容器重建数据不丢。

本地非 Docker 运行：

```bash
pip install -r requirements.txt
APP_DB_PATH=./data/app.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 网页操作

1. 分别选择甲、乙 CSV → 点“解析表头”→ 选择样品号列、测值列，填写换算系数；
2. 点“提交并整体切换当前结果”，页面展示纳入结果表与缺报/超差原因表；
3. 点“下载统一结果 CSV”下载**当前所见版本**的快照（UTF-8 BOM，Excel 直开不乱码）；
4. 修改映射后点“重算并切换版本”即从两份原件重算；替换文件则重新走第 1、2 步。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/preview` | 解析单个 CSV 的表头/行数（不落库） |
| GET | `/api/state?version=N` | 查询版本状态（默认当前版本） |
| POST | `/api/submit` | 两份 CSV + 映射的 multipart 表单提交，支持 `expected_version` |
| POST | `/api/remap` | 仅改映射，从原始文件重算（JSON，必带 `expected_version`） |
| GET | `/api/download?version=N` | 下载指定版本的结果 CSV（默认当前版本） |
| GET | `/healthz` | 健康检查 |

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

覆盖：Decimal 边界（差值恰好 0.05 通过 / 0.0501 拒收）、重复样品号拒收、
物理行号（空行/引号跨行不错位）、500 行上限、缺报与超差原因、
乐观锁 409、非法提交保留旧版、remap 重算、版本固定下载、双线程并发提交仅一个生效。

## 目录

```
app/logic.py        # CSV 解析、校验、Decimal 比对（纯函数）
app/db.py           # SQLite 版本快照、事务与乐观锁
app/main.py         # FastAPI 路由
app/static/index.html  # 单页前端（原生 JS，无外部依赖）
tests/              # pytest 单元与 API 测试
examples/           # 示例 CSV
```
