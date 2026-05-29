# AIComing 上游接入契约

> 平台把用户请求转发到供应商端点时的真实行为。判定兼容性、生成探测结论时以此为准。

## 转发契约（所有接口通用）

平台收到用户请求后，**不透传用户的 header**，重建一个干净请求打给供应商：
- URL = `{你的BaseURL}{path_prefix||/v1}{接口后缀}`（`path_prefix=__raw__` 时 BaseURL 即完整地址）
- `Authorization: Bearer {你的key}`（或按 `auth_type`：header=`X-API-Key`，query=`?key=`）
- `Content-Type` + `Accept: application/json`
- 请求体里的 `model` 换成你配置的 `upstream_model`，**其余字段原样透传**

## 5 条核心适配点

1. **标准 OpenAI 路径**：`/chat/completions`、`/images/generations`、`/images/edits`、`/embeddings` 等。
2. **流式最后一个 chunk 必须带 `usage`**（`prompt_tokens/completion_tokens/total_tokens`）；缺了平台会估算 token，计费可能不准。
3. **图片响应** `url` 或 `b64_json` 都可（平台整段透传、按请求 `n` 计费，不强制 b64）。
4. **上游故障返回 502/500，不要返回 400** —— 400 平台当用户错、不重试；502/500 才触发故障转移。
5. **端点要能从香港访问**（平台服务器在香港），否则探活 fail、无法上架。

## 图片模型契约

- 平台对外只暴露 `gpt-image-2`，按用户 `size` 自动分 1k/2k/4k 档路由到不同端点。供应商按端点 `image_resolution` 声明本端点的最大分辨率。
- **文生图**：`POST /v1/images/generations`（JSON）。
- **图生图（multipart）**：`POST /v1/images/edits`，字段 `model`/`prompt`/`image`(参考图文件，**可重复=多图，也可能是 `image[]`，两种都要认**)/`size`/`n`/`mask`。文件二进制原样透传。
- **edit URL 推导**：若端点是 `__raw__` 且只配了 `.../images/generations`，平台图生图时自动把 `generations` 换成 `edits`。供应商必须保证 `/images/edits` 同级可达。

## 兼容性判定速查

**判定一律基于"完整 URL 上的真实探测结果"，不看模型名。** 模型叫 gemini/claude 不代表不兼容——大多数中转商的 gemini/claude 模型本来就是 OpenAI 格式暴露的，探测能过就兼容。

| 实测现象 | 判定 | 处理 |
|---------|------|------|
| 主接口 200 且**响应是 OpenAI 结构**(chat 有 `choices`、图片有 data 等) | ✅ compatible | 可注册 |
| 缺 usage / 图片返回 url / GET /models 不可用 | ⚠️ 不阻断 | 注册，报告里注明 |
| 主接口非 200 | ❌ unusable | 路径/认证/格式不对，需供应商修上游 |
| 200 但**响应不是 OpenAI 结构**(如 Gemini 的 `candidates`、Anthropic 的 `content`) | ❌ unusable | 见下方"协议"说明 |

### 关于协议（路径 vs 请求体 vs 响应）
- **路径**：用完整 URL + `path_prefix=__raw__` 原样打，路径差异天然解决。注册时 `inferEndpointProtocol` 还会从 URL 自动认出协议（URL 含 `/v1beta/models/` 或 `:generateContent`→gemini；以 `/v1/messages` 结尾→anthropic）。
- **请求体翻译**：平台**有** OpenAI→Gemini / OpenAI→Anthropic 的请求翻译（按协议触发）。
- **响应回译**：平台**没有**把 Gemini/Anthropic 原生响应翻回 OpenAI 结构。所以**真·原生协议端点**（响应是 `candidates`/`content`）即使请求翻过去了，OpenAI 客户端也会拿到非标准响应 → 判 unusable，如实告知需上游提供 OpenAI 格式响应。
- **结论**：OpenAI 格式暴露的模型（无论叫什么名）→ 完整 URL 直接兼容；只有**响应非 OpenAI 结构**的真原生端点才不兼容（受限于"响应不回译"，非 URL 问题）。

### 能力位（tools/vision/json）
skill 的探测**不测**能力位（function calling / 视觉 / JSON 模式）。平台有**服务端定时 probe**，端点上架后会自动探测并写入 `probe_capabilities`，用于按能力路由。所以无需在上架时探这些；只是上架初期能力路由可能还没探准，属正常。
