---
name: aicoming-provider-onboard
description: 帮 API 供应商在 AIComing 供应商平台做两件事——【提交模型】把上游模型上架（落 pending 待审），【管理模型】对已上架模型改价/改配置、下架、重新上架、删除。用供应商自己的 token 操作（模型自动归属其商家）。触发场景：(1)"上架/提交模型到 aicoming""供应商对接""onboard provider"；(2)"改价/改配置/下架/重新上架/删除我的模型"；(3) 需要把某个上游 API 的模型接入或管理 AIComing。
---

# AIComing 供应商上架与管理

这个 skill 只做两件事，开场先问供应商要做哪件：

- **A. 提交模型** —— 把上游一个模型上架（→ pending，待管理员审核）。
- **B. 管理模型** —— 对已有模型改价/改配置、下架、重新上架、删除。

## 三条铁律

1. **能自动抓的就替供应商抓全，别让他手填**：模型清单、类型、厂商 slug、完整 URL、认证方式、上游成本——全部用脚本一次探出来。真正需要供应商给的只有：上游连接信息（base/key）、可选的中转令牌、以及他的**商业决定**（上哪些模型、加价/售价）。这些**一次问一项、等回答**，每项给一句话示例。
2. **绝不替供应商做商业决定或替他确认该确认的值**：上哪些模型、加价率/售价**必须他定**；探到的成本只是“建议”，要他点头。token/base/key 等也由他给——即使你能查库/解密，也不自行填。账号密码只用于换 token，**不存储、不打印**。
3. **任何写操作（提交/改价/下架/删除）前，把将要提交的请求体给供应商确认再发**。删除等不可逆操作尤其要确认。

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

目标：供应商**只给 base + key（+ 可选中转令牌）**，脚本一次把模型、类型、厂商、完整 URL、认证、成本全抓出来，并按加价率算好建议售价；供应商只需挑模型、定加价、确认即上架。

### A1. 收集连接信息（逐项问，等回答）
1. 上游 **Base URL**？（host 或带 `/v1`/`/v2` 都行，不强求到 /v1，脚本自动判断补不补）
2. 上游 **API key**？
3.（可选）能否拿到中转的**「系统访问令牌」**用来自动抓成本价？——在其中转控制台「个人设置 → 生成系统访问令牌」复制（**不是** sk- 开头的 API key）。给了能自动带出成本；给不了就到 A3 手报成本。

### A2. 一条命令出完整计划表
跑探测+抓价（**不传 --model 即全量发现**）：
```bash
python scripts/probe_upstream.py --base <BASE_URL> --key <KEY> --pricing [--pricing-token <系统访问令牌>] [--pricing-url <价目URL>] [--markup 0.2] [--rate 7.2]
```
- **抓价兼容多家中转**，自动按这个顺序合并三种价格来源（`pricing.sources` 会列出命中了哪些）：
  1. **价格内联在 `/v1/models`**（OpenRouter、LiteLLM、自建网关）——免令牌，抓 models 时白捡，覆盖面最广。
  2. **new-api/one-api 的 `/api/pricing`**（比率制）——公开部署直接抓；需令牌的（如 ccapi）带 `--pricing-token`（中转「系统访问令牌」，非 sk- key）。自动同时试 API 域名和根域名。
  3. **供应商给的单个价目 URL**——带 `--pricing-url`，自动判形态。
- `--markup 0.2` = 在抓到的成本上加价 20% 自动算**建议售价**。先问供应商想用多少加价率再带上；不确定可先不带，拿到成本再问。
- 输出每个模型：`model_type`、`assessment.verdict`、`register_hint`（**完整 URL** + `__raw__` + `auth_type` + 自动分辨的 `model_vendor_slug` + 抓到的 `upstream_input_cost/output_cost` + `suggested_input_price/output_price` + `pricing_basis`）。`auth_type` 也自动探出，供应商不用手填。
- 图片模型要实测 gen+edit 加 `--image`（**真实生图、产生费用**，先征得同意）；判 compatible 需文生图+图生图都过。判定与契约见 `references/aicoming-contract.md`。
- 没有 /models 接口（脚本 `note` 提示）→ 让供应商**直接报模型清单**，再 `--model <名>` 逐个探，**不要爬网站**。

### A3. 把计划表讲给供应商，敲定价格
逐模型用中文列出：✅compatible / ⚠️needs_attention / ❌unusable（按实测，不看名字），加每个的**成本 + 建议售价**。
- **❌unusable / ⚠️needs_attention 的模型——主动告诉供应商怎么改，别只报"不兼容"**：拿该模型 `assessment.issues` 里的每条症状，对照 `references/adaptation-guide.md` 的「对症适配表」，把**原因 + 在你上游怎么改 + 自测 curl** 讲清楚（适配在供应商侧做，平台不为单家改）。常见：主接口非 200（路径/认证/格式）、响应非 OpenAI 结构（原生协议需供应商提供 OpenAI 格式入口）、流式缺 usage、图生图 edits 不同级。供应商改好、用自测 curl 跑通后，重跑探测确认 ✅ 再提交。
- **抓到成本** → 给出建议售价（成本×(1+加价)），让供应商确认或改加价率。
- **成本没抓到时的兜底阶梯**（`pricing_basis=需手填` 的模型，**逐级往下试，都是要真实数据、不猜价**）：
  1. 让供应商提供中转的**「系统访问令牌」**→ 重跑加 `--pricing-token`（多数 new-api 这步就拿到了）。
  2. 让供应商给一个**价目接口/页面 URL**（他中转控制台的定价接口）→ 重跑加 `--pricing-url`。
  3. 让供应商**直接粘贴中转控制台的价目**（截图/表格/JSON）→ 你按单位换算后填入。
  4. 都没有 → 供应商**口头报上游成本**（他买的进价，他最清楚）。
  5. 实在给不出 → **不猜价**：存草稿（`POST /provider/drafts`）留住已探配置，或跳过该模型，收尾标「待补价」。
- **单位/币种讲清**（否则差千倍）：token 类 **¥/百万 token**、image **¥/张**、按次 **¥/次**，**默认人民币**；上游报价是美元就 `python scripts/usd_to_cny.py --rate <汇率> '<USD价格JSON>'` 折算，折算前后给供应商对照确认。
- **售价/加价是供应商的决定**，必须他点头；缺价**绝不**填 0 或猜（后端会回退默认价、算错账）。
- `model_vendor_slug` 已自动分辨（`vendor_basis` 说明依据），优先复用平台已有同名模型的 slug；**仅当为空**才问供应商属于哪家（openai/anthropic/google/deepseek/qwen/zhipu-ai/xai/midjourney…）。

### A4. 查重 → 确认 → 提交（逐模型）
1. 查重：`GET /api/v1/provider/models`（带 token）。已 active → **警告**：重新提交会删旧端点、打回 pending、期间掉线；要改配置走 **B（管理模型）**而非重提交。
2. 用 `register_hint`（完整 `upstream_url` + `path_prefix=__raw__` + `auth_type` + `model_vendor_slug` + 定价 CNY；图片带 `upstream_edit_url`/`upstream_edit_model`）拼 `providerModelRequest`（字段见 `references/onboard-api.md`）。
3. **请求体给供应商确认后** POST：
   ```bash
   curl -sS -X POST https://api.aicoming.top/api/v1/provider/models \
     -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
     -d '<providerModelRequest JSON>'
   ```
4. 端点落 `status=pending`，需管理员审核才上线。记录返回，失败按实际响应调字段，再下一个。

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
- `references/adaptation-guide.md` —— **供应商面向的适配手册**：拆包后请求形态 + 对症适配表（探测症状→怎么改）+ 自测 curl。模型不兼容/有警告时发给供应商照着改。
- `scripts/probe_upstream.py` —— 提交主力：全量发现模型 + 判兼容 + 自动分辨厂商 + 完整 URL 注册建议；加 `--pricing [--pricing-token] [--markup]` 一并抓上游成本并算建议售价（含并发与图片实测）。
- `scripts/fetch_pricing.py` —— 多平台抓上游成本（折人民币）：认 ① /v1/models 内联价(OpenRouter/LiteLLM)、② new-api/one-api `/api/pricing`(比率,支持系统访问令牌、自动猜 API/根域名)、③ 供应商给的单个价目 URL(`--url`)；被 probe 复用，也可单独跑。
- `scripts/usd_to_cny.py` —— 美元定价按汇率折人民币。
