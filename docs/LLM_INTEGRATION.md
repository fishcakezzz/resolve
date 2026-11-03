# 大模型 API 接入说明

ResolveFlow 已预留兼容 Chat Completions 的拆案草稿、调查字段选择和候选方案接口。模型只生成待确认的 Subcase 草稿、只读查询选择与候选动作；订单归属、金额、审批和整案预算仍由 `Policy Engine` 与执行层校验。

## 需要提供的信息

1. 服务商名称和 API 文档或请求/响应示例。
2. Base URL（例如 `https://服务地址/v1`）或完整 Chat Completions URL。
3. 模型 ID。
4. 鉴权方式（默认 `Authorization: Bearer <API Key>`）。
5. 是否支持 `response_format: {"type":"json_object"}`。
6. API Key。请放在本地 `.env` 或部署环境的密钥配置中，不要写入代码或提交到仓库。

如果服务商使用与 Chat Completions 不同的接口格式，只需在 `resolveflow/llm.py` 中新增一个适配器；LangGraph 编排与 Policy Engine 不需要改动。

## 配置

复制项目根目录的 `.env.example` 为 `.env`，填写以下字段：

```dotenv
RESOLVE_LLM_BASE_URL=https://provider.example/v1
RESOLVE_LLM_MODEL=your-model-id
RESOLVE_LLM_API_KEY=your-secret
RESOLVE_LLM_TIMEOUT_SECONDS=20
RESOLVE_LLM_JSON_MODE=false
```

如果服务商给的是完整地址，改用 `RESOLVE_LLM_CHAT_URL`，并留空 `RESOLVE_LLM_BASE_URL`。两个地址只能配置一个。服务启动后，调用 `GET /api/llm/status` 检查模式；`remote` 表示配置齐全，不代表服务商已通过连通性验证。

## 请求契约

系统向 Chat Completions 接口发送 `model` 与两条 `messages`。拆案请求的用户消息包含原始诉求；调查请求包含当前角色、允许查询的剩余字段和已观察到的字段状态；候选方案请求包含当前 Subcase 的类型、申报金额（整数分）、SKU 和已验证证据。可选 JSON 模式会附加 `response_format`。完整请求会在用户提供服务商后按其文档联调。

调用 `POST /api/case-drafts` 时，模型消息 `choices[0].message.content` 的示例：

```json
{"issues":[{"issue_type":"MISSING_ITEM","requested_amount_cents":0,"sku":"SKU-1","depends_on":[],"reason":"少发商品"}]}
```

问题类型只接受 `MISSING_ITEM`、`LATE_DELIVERY`、`DUPLICATE_CHARGE`；依赖使用前序问题的零基索引。响应经过 Schema 和依赖校验后只作为草稿，需人工确认并调用 `POST /api/cases` 才会持久化。

调用 `POST /api/cases/{id}/analyze` 时，履约和资金调查角色会逐步选择允许的只读证据字段，模型输出格式为 `{"field":"payment_status"}`。每个角色只能选择任务给出的 `remaining_fields`；越权字段或无效格式使本次分析失败，不会创建候选方案。未配置模型时按固定字段顺序读取已保存的证据快照。当前调查工具不会直接发起新的外部 HTTP 查询，外部证据仍通过 `/collect-evidence` 导入。

调用 `POST /api/cases/{id}/analyze` 时，模型消息 `choices[0].message.content` 必须是纯 JSON 对象，例如：

```json
{"action_type":"REFUND","amount_cents":1000,"reason":"物流延误，建议按政策补偿"}
```

`action_type` 只接受 `REFUND` 或 `RESHIP`，`amount_cents` 必须是非负整数，`reason` 必须为非空字符串。额外字段、文本包裹的 JSON、类型错误和接口异常会使分析请求失败，不会自动转成可执行动作。模型给出的金额或动作即使格式合法，也会继续接受业务策略校验。

## 接入验证

1. 填写配置后启动 API，确认 `GET /api/llm/status` 返回 `remote` 和正确模型名。
2. 调用 `POST /api/case-drafts` 检查多诉求拆案，确认草稿后创建一个演示 Case，再调用 `POST /api/cases/{id}/analyze`。配置模型后，一次分析可能触发多次调查字段选择请求及候选方案请求。
3. 检查候选方案、Policy Decision 和时间线；再验证金额越界、模型超时、非法响应时没有动作进入 Outbox。
4. 执行 `python -m pytest -q`。现有测试使用模拟 HTTP 服务，不消耗真实模型额度。
