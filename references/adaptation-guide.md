# 给供应商：如何适配 AIComing（出现不兼容/警告时，照这个改）

> 用法：探测报出 ⚠️/❌ 时，agent 按下面「对症适配表」找到对应症状，把**原因 + 怎么改 + 自测 curl** 讲给供应商；供应商在**自己上游**改好、用自测 curl 跑通后，再回来重跑探测/走变更请求。
> 核心原则：**适配在供应商侧做**（让上游变成标准 OpenAI 格式），AIComing 不为单个供应商改平台。

## 一、AIComing 会怎么调用你（拆包后的真实请求）

平台收到用户请求后**不透传用户 header**，重建一个干净请求打到你的端点：URL=你配置的完整地址，`Authorization: Bearer 你的key`（或按 auth_type 用 `X-API-Key` / `?key=`），请求体里 `model` 换成你的 `upstream_model`、**其余字段原样透传**。你只要在这个形态上返回**标准 OpenAI 响应**即可。

**chat 非流式** → `POST .../chat/completions`
```json
{"model":"你的模型","messages":[{"role":"user","content":"hi"}],"max_tokens":64}
```
期望响应（必须有 `choices`，最好有 `usage`）：
```json
{"id":"...","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"..."},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":5,"total_tokens":8}}
```

**chat 流式** → 同路径带 `"stream":true`。期望标准 SSE：每行 `data: {...chunk...}`，chunk 有 `choices[].delta`，**最后一个数据 chunk 带 `usage`**，最后 `data: [DONE]`。

**文生图** → `POST .../images/generations`
```json
{"model":"你的模型","prompt":"a red circle","n":1,"size":"1024x1024"}
```
期望 `{"data":[{"b64_json":"..."}]}` 或 `{"data":[{"url":"..."}]}`（两者都行）。

**图生图（multipart）** → `POST .../images/edits`，`multipart/form-data`，字段：`model`、`prompt`、`image`（参考图文件，**可能重复，也可能叫 `image[]`，两种都要收**）、`size`、`n`、可选 `mask`。期望同文生图的响应结构。

## 二、对症适配表（探测报到什么 → 怎么改）

| 探测症状 | 原因 | 在你上游怎么改 | 改完自测 |
|---------|------|---------------|---------|
| 主接口 **404** | 路径不是标准 OpenAI 后缀 | 暴露 `/chat/completions`、`/images/generations`、`/images/edits`、`/embeddings`（或把完整可用地址告诉我们，按 `__raw__` 原样打） | 自测 curl ① |
| 主接口 **401/403** | 认证方式对不上 | 确认接受 `Authorization: Bearer <key>`；若用 `X-API-Key` 或 `?key=`，告诉我们好配 `auth_type` | 自测 curl ① |
| 主接口 **400** | 不收 OpenAI 标准字段 | 接受标准 OpenAI body（`messages`/`max_tokens` 等）；o1/o3 类若只认 `max_completion_tokens` 请说明 | 自测 curl ① |
| 200 但**响应不是 OpenAI 结构**（出现 `candidates`/`content` 等） | 你的端点是 Gemini/Anthropic **原生协议**。平台翻译请求体，但**不回译响应** | 提供一个**OpenAI 格式的响应入口**（响应顶层是 `choices` + `usage`）。原生协议端点上不了架 | 自测 curl ① 看是否有 `choices` |
| 流式**最后 chunk 缺 usage** | 没在结尾返回用量 | 支持 `stream_options:{"include_usage":true}`，或自己在最后一个 chunk 带 `usage`。否则平台估算 token、计费可能不准（不阻断） | 自测 curl ②（看末尾 chunk） |
| 非流式**缺 usage** | 响应没带 usage | 在响应里带 `usage`（不阻断，但建议补，计费更准） | 自测 curl ① |
| **图生图 /images/edits 不可达** | edits 不在 generations 同级 | 让 `/images/edits` 与 `/images/generations` **同级可达**；若不同级，把 edits 完整地址给我们配 `upstream_edit_url` | 自测 curl ④ |
| 图生图**只认 image[]** | 字段名差异 | 收 `image` 即可；只认 `image[]` 也行（平台两种都转发），告知一下即可（不阻断） | 自测 curl ④ |
| 图片**返回 url** | 非 b64 | 可接受，平台整段透传（不阻断，告知用户可能拿到 url 即可） | — |
| 探活 **fail / 超时** | 香港访问不到，或上游故障返 400 | ① 端点要能**从香港访问**；② 上游故障返 **502/500**（会触发故障转移），**不要返 400**（平台当用户错、不重试） | 从香港侧跑自测 curl ① |

## 三、自测 curl（供应商改完先自己跑通，再回来）

把 `<URL>`/`<KEY>`/`<MODEL>` 换成你的值。**响应里出现 `choices`/`data` 即说明 OpenAI 格式 OK。**

① chat 非流式
```bash
curl -sS <URL>/chat/completions -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"model":"<MODEL>","messages":[{"role":"user","content":"ping"}],"max_tokens":16}'
```
② chat 流式（看最后一个非 [DONE] chunk 是否有 usage）
```bash
curl -sN <URL>/chat/completions -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"model":"<MODEL>","messages":[{"role":"user","content":"count to 3"}],"stream":true,"stream_options":{"include_usage":true}}'
```
③ 文生图
```bash
curl -sS <URL>/images/generations -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"model":"<MODEL>","prompt":"a red circle on white","n":1,"size":"1024x1024"}'
```
④ 图生图（multipart，需一张本地 ref.png）
```bash
curl -sS <URL>/images/edits -H "Authorization: Bearer <KEY>" \
  -F model="<MODEL>" -F prompt="make it a gradient" -F size="1024x1024" -F image=@ref.png
```

## 四、改完怎么回来

- **还没上架**：让 agent 重跑 `probe_upstream.py` 确认现在 ✅，再走提交。
- **已上架后要改**：走变更请求（见 `onboard-api.md`：active 端点用 `change-requests type=edit`）。
