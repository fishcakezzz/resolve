# ResolveFlow

企业售后与订单异常闭环 Agent 平台的**本地可运行演示版**。实现重点是 Parent Case / Subcase、证据版本、确定性策略、人工审批、整案预算约束、事务 Outbox 与幂等执行。产品目标和完整需求见 [PRD](ResolveFlow_PRD.md)。

## Agent流程

1. 创建主 Case，拆成多个带依赖关系的 Subcase。
2. 导入本地模拟的 CRM / OMS / Payment / Logistics / Knowledge 证据，或调用可配置的只读 HTTP 证据适配器；保存来源与版本，标记跨来源冲突。
3. 使用 LangGraph 分析节点生成候选动作；本地默认使用确定性规划器，可通过环境变量启用兼容 Chat Completions 协议的模型接口。
4. 用 Policy Engine 校验订单归属、支付状态、重复扣款/缺件/延误证明、退款余额与审批阈值。
5. 修改证据时使未执行方案和审批失效；人工批准、驳回或修改参数后重新校验。
6. 执行前检查 Subcase 依赖、整案累计退款和重复补发，再在同一数据库事务中写入 Outbox。
7. 独立 Worker 幂等执行模拟退款/补发；所有必需 Subcase 成功后主 Case 才结束。
8. 提供 Case、证据、时间线和执行结果查询接口。


## 本地运行

需要 Python 3.12。Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn resolveflow.api:app --reload
```

另开终端启动 Worker：

```powershell
.\.venv\Scripts\python.exe -m resolveflow.worker
```

默认使用当前目录的 SQLite 数据库。API 文档位于 `http://127.0.0.1:8000/docs`。如果已安装 Docker，先将 `.env.example` 复制为 `.env`，设置 URL 安全的 `RESOLVE_POSTGRES_PASSWORD`，再运行 `docker compose up --build`；这会使用 PostgreSQL 和独立 Worker。本次开发环境未提供 Docker，因此 Compose 尚未实测。

### 可选外部接口

- `RESOLVE_CRM_EVIDENCE_URL`、`RESOLVE_OMS_EVIDENCE_URL`、`RESOLVE_PAYMENT_EVIDENCE_URL`、`RESOLVE_LOGISTICS_EVIDENCE_URL`、`RESOLVE_KNOWLEDGE_EVIDENCE_URL`：配置只读证据接口。调用 `POST /api/cases/{case_id}/collect-evidence` 时并发请求已配置的来源，查询参数为 `customer_id`、`order_id`。接口需返回 `{"source_id":"...","facts":{"field":value}}`；冲突字段不会自动写入证据。
- 模型接口：复制 `.env.example` 为本地 `.env` 并填写 `RESOLVE_LLM_BASE_URL` **或** `RESOLVE_LLM_CHAT_URL`、`RESOLVE_LLM_MODEL`、`RESOLVE_LLM_API_KEY`。`BASE_URL` 应包含服务商要求的版本路径，例如 `/v1`；系统会追加 `/chat/completions`。`CHAT_URL` 是完整的 Chat Completions 地址。默认使用 `Authorization: Bearer <API Key>`，请求和响应兼容 Chat Completions 结构。`RESOLVE_LLM_JSON_MODE=true` 时发送 `response_format: {"type":"json_object"}`，仅在服务商支持时启用。
- 本地运行会读取项目目录中的 `.env`；Docker Compose 也会读取同目录的 `.env`。通过 `GET /api/llm/status` 可查看 `demo`、`remote` 或 `misconfigured` 状态，响应不包含密钥。模型只生成候选动作，策略和整案约束仍由程序校验；模型超时、HTTP 错误或响应 Schema 不合法会返回 502，Case 不会产生可执行动作。

模型服务的配置字段、请求格式和联调步骤见 [大模型 API 接入说明](docs/LLM_INTEGRATION.md)。

## 演示请求

`POST /api/cases`：

```json
{
  "request_key": "demo-order-1001",
  "customer_id": "customer-1",
  "order_id": "order-1001",
  "issues": [
    {"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-1"},
    {"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000},
    {"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 4000}
  ],
  "demo_facts": {
    "order_owner": {"value": "customer-1", "source_system": "CRM", "source_id": "crm-1"},
    "payment_status": {"value": "PAID", "source_system": "PAYMENT", "source_id": "payment-1"},
    "order_total_cents": {"value": 10000, "source_system": "OMS", "source_id": "order-1001"},
    "refunded_cents": {"value": 0, "source_system": "PAYMENT", "source_id": "payment-1"},
    "missing_item_confirmed": {"value": true, "source_system": "OMS", "source_id": "order-1001"},
    "logistics_status": {"value": "LATE", "source_system": "LOGISTICS", "source_id": "shipment-1"},
    "duplicate_charge_confirmed": {"value": true, "source_system": "PAYMENT", "source_id": "payment-2"}
  }
}
```

然后调用：

1. `POST /api/cases/{case_id}/analyze`，得到方案和策略结果。
2. 如方案状态为 `PENDING_APPROVAL`，调用 `POST /api/cases/{case_id}/subcases/{key}/approval`。
3. 从 `GET /api/cases/{case_id}` 读取 `version`，调用 `POST /api/cases/{case_id}/execute`，请求体 `{"expected_case_version": 版本号}`。
4. Worker 处理后，通过 `GET /api/cases/{case_id}`、`/timeline`、`/executions` 查看结果。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前测试覆盖：复杂 Case 结案、证据更新后审批失效、整案退款上限、依赖顺序、请求幂等、Worker 崩溃后恢复、跨来源证据冲突与接口失败降级，以及模型请求格式、异常响应和模型建议越权金额的拦截。
