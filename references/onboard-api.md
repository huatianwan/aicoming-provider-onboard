# AIComing 供应商上架 API 参考

> 这些接口字段来自后端真实代码（`internal/handler/auth/provider_ops.go`）。
> 调用前以线上实际响应为准；字段或路由若对不上，按实际返回调整，不要硬套。

## 基础

- API Base：`https://api.aicoming.top`

### 取得 token（第一步，按注册方式两路）
所有后续请求都用 `Authorization: Bearer <token>`。token 决定模型归属（后端按 token 的 user_id 解析其 provider，自动落本人商家）。

- **账号密码用户**：`POST /api/v1/auth/login` `{"username","password"}` → `{"data":{"token"}}`。密码仅用于换 token，不存储。登录响应 `data.user.has_provider` 表明是否供应商。
- **Google / GitHub（OAuth）用户**：**没有密码，CLI 无法替其登录**。让其在浏览器登录 `aicoming.top` 控制台后，复制 localStorage 里 `aic_token` 的值（即 JWT）粘贴使用。这条对所有登录方式都通用。
- OAuth 走 `/api/v1/auth/oauth/:provider/callback`（浏览器重定向流程），无法在 CLI 完成——所以 OAuth 用户只能粘贴已登录的 token。

**校验供应商资格**（任一路拿到 token 后）：`GET /api/v1/provider/dashboard`（带 Bearer）→ 返回 `provider` 非空即供应商；为 null/401 → 不是供应商，引导去申请页 `https://aicoming.top/merchant-apply.html`（申请接口 `POST /api/v1/merchants/apply`，含证件/头像上传，让供应商在网页做，skill 不代办）。非供应商调 `POST /api/v1/provider/models` 会返回 `404 provider profile required`。

### 完整 URL 约定（重要）
注册每个模型时，**提交完整的端点 URL + `path_prefix="__raw__"`**，让平台原样使用、不再拼接：
- 对话：`upstream_url = {base}/chat/completions`
- 图片：`upstream_url = {base}/images/generations`，`upstream_edit_url = {base}/images/edits`
- 嵌入：`upstream_url = {base}/embeddings`

探测脚本的 `register_hint` 已按此生成，直接用即可。

## 注册一个模型端点

`POST /api/v1/provider/models`

请求体字段（`providerModelRequest`，全部可填，未填用默认）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 平台模型展示名 |
| `slug` | string | 模型标识 |
| `model_vendor_slug` 或 `model_vendor_id` | string/uint | **模型厂商，必填**（只给 `model_vendor_name` 会 400）。常用 slug：`openai` `anthropic` `google` `deepseek` `qwen` `zhipu-ai` `xai` `midjourney` 等 |
| `type` | string | 模型类型（chat/image/embedding 等） |
| `category` | string | 分类 |
| `upstream_url` | string | 你的上游 Base URL |
| `upstream_edit_url` | string | 图生图（/images/edits）的完整 URL；留空则平台按 `generations→edits` 自动推导 |
| `upstream_model` | string | 你上游的真实模型名（留空=用平台模型名） |
| `upstream_edit_model` | string | 图生图使用的模型名；留空则回退用 `upstream_model` |
| `path_prefix` | string | 路径前缀，默认 `/v1`；填 `__raw__` 表示 upstream_url 是完整地址 |
| `auth_type` | string | `bearer`(默认) / `header`(x-api-key) / `query`(?key=) |
| `api_key` | string | 你的上游 key（后端加密存储） |
| `billing_type` | string | `token` / `image` / `call` |
| `image_price` / `call_price` | float | 图片单价 / 按次单价 |
| `input_price` / `output_price` / `cache_price` | float | 用户价（¥/1M token） |
| `cache_create_5m_price` / `cache_create_1h_price` | float | 缓存写入价 |
| `upstream_input_cost` / `upstream_output_cost` / `upstream_cache_cost` | float | 你的上游成本（用于结算毛利，不对用户展示） |
| `image_resolution` | string | 图片端点支持的最大分辨率，如 `1024x1024` |
| `probe_result` / `probe_error_code` / `probe_message` / `probe_latency_ms` / `probe_status_code` | — | 探测结果（可由 probe 脚本填入） |
| `note` | string | 备注 |

**行为**：创建后端点 `status="pending"`，`protocol` 由 `upstream_url`+`path_prefix` 自动推导，`currency="CNY"`。**需管理员审核后才 `active` 上架。**

> ⚠️ **重复注册有破坏性**：对"同 provider + 同 model"再次调用本接口，后端会**先删除该 provider 现有的同模型端点、再建新的（pending）**。若该模型已 `active` 上线，重注册会让它**掉线、打回待审**。所以注册前应 `GET /api/v1/provider/models` 查重；要改已上线端点的配置/价格，走「变更请求」(`/provider/change-requests`)，不要重注册。

> 价格单位：`input/output/cache_price` 与 `upstream_*_cost` 均为 **¥/1M token**；`image_price` 为 **¥/张**；`call_price` 为 **¥/次**。美元报价先用 `usd_to_cny.py` 折算。

## 图片编辑端点（图生图）

提交接口已支持 `upstream_edit_url` / `upstream_edit_model`，可直接填：
- 探测确认了上游 `/images/edits` 的真实地址后，把它填进 `upstream_edit_url`（探到的 edit model 填 `upstream_edit_model`）。
- 留空时平台回退到推导：`path_prefix=__raw__` 且 `upstream_url` 指向 `.../v1/images/generations` → 自动换成 `edits`（适用于同级上游，如 ccapi）。

> 注：此能力依赖后端已部署对应版本（提交接口 + change-request 已加这两个字段并贯通到 relay）。若线上尚未部署该版本，填了也会被忽略，仍走推导/管理员补。

## 草稿（可选）

- `POST /api/v1/provider/drafts` `{name, form_data}`（form_data 为表单 JSON 字符串）
- `GET /api/v1/provider/drafts` / `PUT /api/v1/provider/drafts/:id` / `DELETE /api/v1/provider/drafts/:id`

## 管理已有端点（改价/改配置/下架/重新上架/删除）

按端点 `status` 选接口（这是前端控制台实际走的约定）：

| 操作 | 接口 | 适用状态 | 是否需审核 |
|------|------|----------|------------|
| 改价/改配置 | `POST /api/v1/provider/change-requests` `{endpoint_id,type:"edit",payload}` | **active** | 是，不掉线，审核后生效 |
| 改价/改配置 | `PUT /api/v1/provider/models/:id`（body 为只含要改字段的 `providerModelRequest`） | pending / 草稿（未上线） | 否，直接改 |
| 下架 | `PUT /api/v1/provider/models/:id` `{"status":"disabled"}`（临时降级用 `"suspended"`） | active | 否，立即隐藏，停止路由 |
| 重新上架 | `POST /api/v1/provider/change-requests` `{endpoint_id,type:"relist",payload:"{}"}` | disabled/suspended | 是，审核后恢复 |
| 删除 | `DELETE /api/v1/provider/models/:id` | **仅** disabled / rejected | 否，不可逆 |

- `change-requests` 的 `payload` 是**改动字段的 JSON 字符串**。`type` 只能是 `edit` 或 `relist`。管理员 `POST /api/v1/admin/change-requests/:id/approve` 后生效。
- 同一 endpoint + 同一 type 已有 `pending` 变更时再提会被拒（"已有相同的待审批请求"）；先 `DELETE /api/v1/provider/change-requests/:id` 撤回（加 `?hard=1` 硬删非 pending 记录）。
- ⚠️ change-request `edit` 映射支持：各价格、`upstream_input_cost/output_cost/cache_cost`、`upstream_model/upstream_url/path_prefix/auth_type`、`api_key`。**不支持** `upstream_edit_url/upstream_edit_model/image_resolution/billing_type`（需管理员直接处理）。
- ⚠️ `DELETE` 仅允许 `status` 已是 `disabled` 或 `rejected`；active 端点会返回"只能删除已下架或已拒绝的模型，请先下架再删除"。删除会连带清理无其它端点引用的孤立 catalog 模型——不可逆。
- 查变更历史：`GET /api/v1/provider/change-requests`。

## 上架后的状态/探活

- 端点 `status`：`pending` → 管理员 approve → `active`；`active` --下架--> `disabled`；`disabled` --relist 审核--> `active`。
- 平台会定期探活；连续失败会把 `route_eligible` 置 0（暂停路由），恢复后自动回归。
