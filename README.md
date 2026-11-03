# ResolveFlow

ResolveFlow 是面向企业售后与订单异常的 Agent 平台。它将复杂诉求拆解为相互关联的 Subcase，围绕证据、策略、审批和执行结果形成可追踪的处理闭环。项目设计见 [PRD](ResolveFlow_PRD.md)。

分析流程按 Case Coordinator、Fulfillment Investigator、Payment Investigator、Resolution Planner 四个角色组织；角色职责、证据权限和调用顺序见 [四角色架构说明](docs/AGENT_ARCHITECTURE.md)。

## Agent流程

1. 输入客户自然语言诉求，生成待确认的 Subcase 草稿；检查金额、SKU 和依赖后创建主 Case。
2. 导入本地模拟的 CRM / OMS / Payment / Logistics / Knowledge 证据，或调用可配置的只读 HTTP 证据适配器；保存来源与版本，标记跨来源冲突。
3. 使用 LangGraph 派发履约和资金调查任务，再由整案方案角色生成候选动作；本地默认使用确定性选择逻辑，可通过环境变量启用兼容 Chat Completions 协议的模型接口。
4. 用 Policy Engine 校验订单归属、支付状态、重复扣款/缺件/延误证明、退款余额与审批阈值。
5. 修改证据时使未执行方案和审批失效；人工批准、驳回或修改参数后重新校验。
6. 执行前展示整案计划，检查 Subcase 依赖、累计退款、重复补发及同 SKU 退款与补发冲突，再在同一数据库事务中写入 Outbox。
7. 独立 Worker 幂等执行模拟退款/补发；所有必需 Subcase 成功后主 Case 才结束，并记录结案事件。
8. 提供 Case、证据、整案计划、结案证明、时间线和执行结果查询接口。

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

默认使用当前目录的 SQLite 数据库。API 文档位于 `http://127.0.0.1:8000/docs`。若使用 Docker，先将 `.env.example` 复制为 `.env`，设置 URL 安全的 `RESOLVE_POSTGRES_PASSWORD`，再运行 `docker compose up --build`；Compose 配置包含 PostgreSQL、API 服务和独立 Worker。

示例请求中的 `demo_facts` 和 Worker 使用的 `DemoAction` 用于本地流程演示。接入真实业务系统时，需要按企业接口协议实现证据查询及退款、补发执行适配器。

### 可选外部接口

- `RESOLVE_CRM_EVIDENCE_URL`、`RESOLVE_OMS_EVIDENCE_URL`、`RESOLVE_PAYMENT_EVIDENCE_URL`、`RESOLVE_LOGISTICS_EVIDENCE_URL`、`RESOLVE_KNOWLEDGE_EVIDENCE_URL`：配置只读证据接口。调用 `POST /api/cases/{case_id}/collect-evidence` 时并发请求已配置的来源，查询参数为 `customer_id`、`order_id`。接口需返回 `{"source_id":"...","facts":{"field":value}}`；冲突字段不会自动写入证据。
- 模型接口：复制 `.env.example` 为本地 `.env` 并填写 `RESOLVE_LLM_BASE_URL` **或** `RESOLVE_LLM_CHAT_URL`、`RESOLVE_LLM_MODEL`、`RESOLVE_LLM_API_KEY`。`BASE_URL` 应包含服务商要求的版本路径，例如 `/v1`；系统会追加 `/chat/completions`。`CHAT_URL` 是完整的 Chat Completions 地址。默认使用 `Authorization: Bearer <API Key>`，请求和响应兼容 Chat Completions 结构。`RESOLVE_LLM_JSON_MODE=true` 时发送 `response_format: {"type":"json_object"}`，仅在服务商支持时启用。
- 本地运行会读取项目目录中的 `.env`；Docker Compose 也会读取同目录的 `.env`。通过 `GET /api/llm/status` 可查看 `demo`、`remote` 或 `misconfigured` 状态，响应不包含密钥。模型只生成候选动作，策略和整案约束仍由程序校验；模型超时、HTTP 错误或响应 Schema 不合法会返回 502，Case 不会产生可执行动作。

模型服务的配置字段、请求格式和联调步骤见 [大模型 API 接入说明](docs/LLM_INTEGRATION.md)。

## 演示请求

先调用 `POST /api/case-drafts` 生成待确认草稿：

```json
{"request_text":"订单少发一件，物流晚到，还重复扣款"}
```

响应包含 `issues`、每个问题的 `required_evidence` 和 `needs_input`。无模型配置时使用关键词规则；配置模型后使用结构化模型输出。请检查并补齐金额、SKU、依赖关系，再将 `issues` 与原始 `request_text` 一起提交到 `POST /api/cases`。草稿本身不会创建 Case 或执行退款。

`POST /api/cases`：

```json
{
  "request_key": "demo-order-1001",
  "customer_id": "customer-1",
  "order_id": "order-1001",
  "request_text": "订单少发一件，物流晚到，还重复扣款",
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
3. 调用 `GET /api/cases/{case_id}/plan` 查看整案预算、各动作阻塞原因和 `can_execute_now`；从响应读取 `case_version`，调用 `POST /api/cases/{case_id}/execute`，请求体 `{"expected_case_version": 版本号}`。执行入口会复用整案校验。
4. Worker 处理后，通过 `GET /api/cases/{case_id}`、`/timeline`、`/executions` 查看结果；调用 `GET /api/cases/{case_id}/closure-proof` 核对每个 Subcase 的证据、策略、审批和模拟外部回执。仅全部条件满足时返回 `verified=true`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖：四角色任务派发与调查证据权限、自然语言拆案与人工确认、整案计划冲突解释、结案证明、复杂 Case 结案、证据更新后审批失效、整案退款上限、依赖顺序、请求幂等、Worker 崩溃后恢复、跨来源证据冲突与接口失败降级，以及模型请求格式、异常响应和模型建议越权金额的拦截。
