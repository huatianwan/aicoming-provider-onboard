---
name: aicoming-provider-onboard
description: 帮 API 供应商在 AIComing 供应商平台做两件事——【提交模型】把上游模型上架（落 pending 待审），【管理模型】对已上架模型改价/改配置、下架、重新上架、删除。用供应商自己的 token 操作（模型自动归属其商家）。触发场景：(1)"上架/提交模型到 aicoming""供应商对接""onboard provider"；(2)"改价/改配置/下架/重新上架/删除我的模型"；(3) 需要把某个上游 API 的模型接入或管理 AIComing。
---

# AIComing 供应商上架与管理

这个 skill 只做两件事，开场先问供应商要做哪件：

- **A. 提交模型** —— 把上游一个模型上架（→ pending，待管理员审核）。
- **B. 管理模型** —— 对已有模型改价/改配置、下架、重新上架、删除。

## 三条铁律

1. **一步一步，一次只问一项**：问完一项停下等回答，再问下一项。绝不一次甩一堆字段。每项给一句话示例。
2. **绝不自己填用户该提供的值**：token、base_url、key、价格……即使你能查库/解密/上下文已有，也**必须让供应商自己给**。这是引导，不是脚本批跑。
3. **任何写操作（提交/改价/下架/删除）前，把将要提交的请求体给供应商确认再发**。账号密码只用于换 token，不存储、不打印；删除等不可逆操作尤其要确认。

## 通用第 0 步 —— 取 token + 校验资格

先拿到供应商 token（决定模型归属，后端按 token 的 user_id 落到本人商家）：

- **账号密码用户**：`POST https://api.aicoming.top/api/v1/auth/login {"username","password"}` → 取 `data.token`。
- **Google/GitHub（OAuth）用户**：没有密码，让其浏览器登录 `aicoming.top` 控制台 → 开发者工具 → localStorage → 复制 `aic_token` 的值粘贴。（此法对所有人通用。）

校验是不是供应商：
```bash
curl -sS https://api.aicoming.top/api/v1/provider/dashboard -H "Authorization: Bearer <TOKEN>"
```
- 有 provider 数据 / `has_provider==true` → 继续。
- null / 401 → 不是供应商，停下，告知去 **https://aicoming.top/merchant-apply.html** 申请入驻（含证件上传，在网页做，不代办）。

---

## A. 提交模型

### A1. 收集这一个模型的信息（逐项问）
1. 上游 **Base URL**？（host 或带 `/v1`/`/v2` 都行，不强求到 /v1）
2. 上游 **API key**？
3. 这次要上的**模型名**（上游真实模型 ID）？
4. 模型**类型**（chat / image / embedding…）？

### A2.（可选）探测验证——拿不准是否兼容时才用
供应商若不确定格式对不对、或想先验证能不能跑通，再用探测脚本；**他清楚自己接口的，可跳过直接填表**。
```bash
python scripts/probe_upstream.py --base <BASE_URL> --key <KEY> --model <模型> [--image]
```
- 输出 `auth_type`（认证方式）、`assessment.verdict`（compatible/needs_attention/unusable）、`register_hint`（完整 URL + `__raw__` + 建议字段）。判定与契约见 `references/aicoming-contract.md`。
- 图片模型加 `--image` 会**真实生图、产生费用**，须先征得同意；判 compatible 需文生图+图生图都过。
- 不传 `--model` 可探 `/models` 全量；没有 /models 接口就让供应商直接报模型清单，**不要爬网站**。

### A3. 定价（强制，逐项问）
- **单位/币种讲清**（否则差千倍）：token 类 **¥/百万 token**（input/output）、image **¥/张**、按次 **¥/次**；**默认人民币**。
- **售价必须问供应商**（他的加价决定，探不出来）。缺价**绝不**填 0 或猜——后端会回退默认价、算错账；给不出价就存草稿（`POST /provider/drafts`）或跳过。
- 报价是美元 → 问当日汇率，`python scripts/usd_to_cny.py --rate <汇率> '<USD价格JSON>'` 折人民币，折算前后给供应商对照确认。
- 想省手填**成本**可选试 `python scripts/fetch_pricing.py --base <中转host> --rate <汇率> --model <模型>`（new-api 公开 pricing 才抓得到，多数需登录则手填）。

### A4. 厂商 slug（必填）
`model_vendor_slug` 必填（只给 name 会 400）。探测的 `register_hint` 已自动分辨（`vendor_basis` 说明依据），通常直接用；**仅当为空**才问供应商属于哪家（openai/anthropic/google/deepseek/qwen/zhipu-ai/xai/midjourney…）。优先复用平台已有同名模型的 slug，别乱起新 slug。

### A5. 查重 → 确认 → 提交
1. 查重：`GET /api/v1/provider/models`（带 token）看这模型是否已存在。已 active → **警告**：重新提交会删旧端点、打回 pending、期间掉线；要改配置应走 **B（管理模型）**而非重提交。
2. 按 `references/onboard-api.md` 的 `providerModelRequest` 拼请求体：完整 `upstream_url` + `path_prefix=__raw__` + `auth_type` + `model_vendor_slug` + 定价（CNY）；图片再带 `upstream_edit_url`/`upstream_edit_model`。
3. **请求体给供应商确认后** POST：
   ```bash
   curl -sS -X POST https://api.aicoming.top/api/v1/provider/models \
     -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
     -d '<providerModelRequest JSON>'
   ```
4. 端点落 `status=pending`，需管理员审核才上线。记录返回，失败按实际响应调字段。

---

## B. 管理模型

### B1. 列出供应商的模型，让他选
```bash
curl -sS https://api.aicoming.top/api/v1/provider/models -H "Authorization: Bearer <TOKEN>"
```
把 `data.items[]` 用中文列出：模型名 / 类型 / **状态**（active 上架中、pending 审核中、disabled/suspended 已下架、rejected 已拒绝）/ 现价 / 端点 id。问供应商：**改哪一个、做什么操作？**（改价改配置 / 下架 / 重新上架 / 删除）

### B2. 按操作走对应接口（注意状态机）

**① 改价 / 改配置**
- **active（已上架）端点 → 走变更请求**（不掉线、审核后生效）：
  ```bash
  curl -sS -X POST https://api.aicoming.top/api/v1/provider/change-requests \
    -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
    -d '{"endpoint_id":<ID>,"type":"edit","payload":"<改动字段的JSON字符串>"}'
  ```
  payload 支持改：各价格、`upstream_input_cost/output_cost/cache_cost`、`upstream_model/upstream_url/path_prefix/auth_type`、`api_key`。**不支持** `upstream_edit_url/upstream_edit_model/image_resolution/billing_type`（这些要管理员直接改）。同一端点同 type 已有 pending 会被拒，先撤回（`DELETE /provider/change-requests/:id`）。
- **pending / 草稿（尚未上线）端点 → 直接改**（`PUT`，无需再审）：
  ```bash
  curl -sS -X PUT https://api.aicoming.top/api/v1/provider/models/<ID> \
    -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
    -d '<只含要改字段的 providerModelRequest JSON>'
  ```
- 改价同样遵守 A3 的单位/币种规则，美元先折人民币。

**② 下架**（从商城和路由隐藏，不再接流量）：
```bash
curl -sS -X PUT https://api.aicoming.top/api/v1/provider/models/<ID> \
  -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{"status":"disabled"}'
```
（临时降级用 `"suspended"`。）

**③ 重新上架**（下架后想恢复）→ 走变更请求，审核后恢复：
```bash
curl -sS -X POST https://api.aicoming.top/api/v1/provider/change-requests \
  -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"endpoint_id":<ID>,"type":"relist","payload":"{}"}'
```

**④ 删除**（彻底移除）→ **必须先下架**（status=disabled/rejected）才能删，不可逆，删前务必确认：
```bash
curl -sS -X DELETE https://api.aicoming.top/api/v1/provider/models/<ID> -H "Authorization: Bearer <TOKEN>"
```

### B3. 收尾
告知结果与下一步：变更/重新上架是 **pending，等管理员审核**才生效；下架/删除立即生效。

---

## 参考资料

- `references/onboard-api.md` —— 登录、提交、变更、下架/删除接口的真实字段、鉴权、状态机、完整 URL 约定。
- `references/aicoming-contract.md` —— 接入契约、5 条适配点、图片/edit 规则、兼容判定（仅探测时需要）。
- `scripts/probe_upstream.py` —— 可选：探测上游、判兼容、给完整 URL 注册建议（含并发与图片实测）。
- `scripts/fetch_pricing.py` —— 可选：从中转 `/api/pricing` 抓上游成本建议（new-api 格式，折人民币）。
- `scripts/usd_to_cny.py` —— 美元定价按汇率折人民币。
