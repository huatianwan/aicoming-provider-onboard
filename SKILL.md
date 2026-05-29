---
name: aicoming-provider-onboard
description: 引导 API 供应商把自己的模型自助上架到 AIComing 供应商平台。用供应商的账号密码登录换取 token（模型自动归属到其商家名下），自动发现其上游全部模型、逐个探测格式/能力、对照 AIComing 契约判定兼容性，把兼容的模型以完整 URL 注册为端点（落 pending 待管理员审核）。触发场景：(1) 供应商想接入/上架模型到 AIComing；(2) 提到"上架模型""接入 aicoming""供应商对接""onboard provider""注册端点到 aicoming"；(3) 需要检测某个上游 API 是否兼容 AIComing。
---

# AIComing 供应商自助上架

引导供应商：登录 → 发现全部模型 → 逐个探测判兼容 → 注册（pending，归属其商家）→ 报告。

## 两条铁律

1. **一步一步引导，一次只问一项**：问完一项，**停下、等供应商回答**，再问下一项。**绝不一次甩一大堆字段让人填**，避免供应商填乱。每问一项给一句话示例。
   - **强制**：每一项输入（账号、密码、base_url、key、定价…）都**必须明确提问并等待用户输入**。即使你（agent）有别的办法拿到这些值（能查库、能解密、上下文里已有），也**绝不允许**自行填入或批量执行——必须让供应商自己提供。这是引导式上架，不是脚本批跑。
   - 每个 `curl`/脚本只在**拿到该步所需的用户输入后**才执行。
2. **注册前必须把请求体给供应商确认**再 POST。所有端点落 `pending`，需管理员审核才上线，不直接进生产路由。
   - 账号密码**只用于调登录接口换 token**，token 用完即用，**不存储、不打印密码**。
   - 图片探测会产生**真实生图费用**，必须先征得同意。

## 工作流（严格按顺序，每步等回答）

### 步骤 1 — 取得 token（按注册方式分两路）
先问：你是**账号密码**注册的，还是用 **Google / GitHub** 登录的？

**A. Google/GitHub（OAuth）用户 —— 没有密码，走粘贴 token（也是通用方式）**
1. 让其在浏览器登录 `https://aicoming.top` 控制台。
2. 让其打开开发者工具 → Application/存储 → localStorage → 复制 key 为 **`aic_token`** 的值，粘贴给你。
3. 这个 token 即身份；OAuth 登录拿不到密码，所以 CLI 无法替他登录，只能他自己登录后给 token。

**B. 账号密码用户 —— 可直接换 token（也可同 A 粘贴 token）**
```bash
curl -sS -X POST https://api.aicoming.top/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<账号>","password":"<密码>"}'
```
取返回 `data.token`。密码仅用于换 token，不保存、不打印。

**两路共同点**：token 决定模型归属——后续注册都用它，后端按 token 的 user_id 解析其 provider，**自动落到本人商家名下**，无法指定别的商家。

**校验供应商资格**（拿到 token 后，任一路都要做）：用 token 调
```bash
curl -sS https://api.aicoming.top/api/v1/provider/dashboard -H "Authorization: Bearer <TOKEN>"
```
- 返回里有 provider 数据 / `has_provider==true` → 是供应商，继续步骤 2。
- `provider` 为 null / 401 / 提示无供应商 → **不是供应商，停下**，告知：
  > 你的账号还不是供应商，先到 **https://aicoming.top/merchant-apply.html** 申请入驻（提交商家资料/证件），管理员审核通过后再回来上架。
- 不替供应商走申请（涉及证件/头像上传，在网页做）。

### 步骤 2 — 上游连接信息（base_url → key）
依次问：
1. 问：你的上游 Base URL？—— 等回答。**不强求到 `/v1`**：给 host(`https://api.xxx.com`)或带前缀(`.../v1`、`.../v2`)都行，探测脚本会自动判断要不要补 `/v1`、并探出真实可用的完整端点。
2. 问：上游 API key？—— 等回答。

### 步骤 3 — 发现并探测全部模型
跑探测脚本（**不传 --model 即探测 `/models` 发现的全部模型**；先不含图片以免花钱）：
```bash
python scripts/probe_upstream.py --base <BASE_URL> --key <KEY>
```
输出 `auth_type`（自动探测的认证方式，bearer/header/query，供应商不用手填）+ `results[]`，每个模型含：`model_type`、`assessment.verdict`、`issues`、`register_hint`（**完整 URL** + `__raw__` + `auth_type` + 建议字段）。判定与契约见 `references/aicoming-contract.md`。

**没有 `/models` 接口时（如 subrouter.ai，脚本 `note` 会提示）**：
1. 先问供应商："你的 API 有列模型的接口吗（如 /models）？" —— 有就给路径，脚本会用上。
2. 没有 → **直接让供应商提供模型清单**（他最清楚自己有哪些模型，这是最快最准的来源）。
3. 然后对清单里每个模型 `--model <模型>` 逐个探测。
4. **不要去爬供应商网站**——每个站结构不同、易变、抓到的可能是展示名而非真实 API 模型 ID，不可靠。顶多在供应商**明确给一个文档/价目页 URL**时，抓那**一个**页面辅助提取候选模型名，且**必须他确认**，绝不全站爬、绝不当权威来源。

若发现里有**图片模型**：问供应商"是否现在实测图片模型（文生图+图生图，会产生几分钱费用）？"——同意后对图片模型补跑：
```bash
python scripts/probe_upstream.py --base <BASE_URL> --key <KEY> --model <图片模型> --image
```
图片模型判 `compatible` 需文生图+图生图都 200。

### 步骤 4 — 呈现兼容性清单
把 `results` 用中文列给供应商（每个模型一行）：
- ✅ `compatible`：可上架。
- ⚠️ `needs_attention`：可上架但有不阻断问题（缺 usage / 返 url / 仅文生图等）——注明。
- ❌ `unusable`：**按完整 URL 实测判定，不看模型名**。两种情况：① 主接口非 200（路径/认证/格式不对）；② 200 但响应不是 OpenAI 结构（真原生协议如 Gemini `candidates`/Anthropic `content`——平台翻请求但**不回译响应**）。如实告知需上游提供 OpenAI 格式入口，不要假装能修。
  - 注意：模型名叫 gemini/claude **不等于** unusable——多数中转商的 gemini/claude 是 OpenAI 格式暴露的，探测能过就兼容。
- `needs_manual_check`/`needs_image_probe`：未覆盖类型或图片未实测，提示补测。

### 步骤 5 — 逐个模型上架（一次一个，别批量）
**对每个可上架的模型，单独走一遍**，不要一次性处理全部：
1. 报这个模型的探测结论，问：这个上架吗？—— 等回答。
2. 问定价。**必须讲清单位**，否则容易差 1000 倍：
   - token 类：输入价 / 输出价 / 上游成本，单位 **¥ 每百万(1M) token**。
   - image 类：每**张**价 + 成本，单位 **¥/张**。
   - 按次类：每**次**单价，单位 **¥/次**。
   - **先问币种**（人民币还是美元）。
   - **价格探测不到，只能问；定价是强制项。** 缺价时**绝不**用 0 或猜的值提交（后端会回退到平台模型默认价、成本算 0 → 算错账）。
   - 供应商一时给不出价 → **不要注册这个模型**。二选一：① 跳过，收尾里标"待补价"；② 存草稿保留已探配置：`curl -sS -X POST https://api.aicoming.top/api/v1/provider/drafts -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{"name":"<模型>","form_data":"<已探到的配置JSON字符串>"}'`，让其拿到价再提交。
3. **若报价是美元**：问当日 USD→CNY 汇率（让其确认），折算：
   ```bash
   python scripts/usd_to_cny.py --rate <汇率> '<USD价格字段JSON>'
   ```
   把折算后 CNY 值用于注册，`note` 写"原始 USD，按 <汇率> 折算"。**折算前后对照给供应商确认**。
4. 用 `register_hint`（**完整 upstream_url + path_prefix=__raw__** + `auth_type`(探测得出) + `model_vendor_slug`；图片再带 `upstream_edit_url`/`upstream_edit_model`）+ 定价 + 探测结果，拼 `providerModelRequest`（字段见 `references/onboard-api.md`）。
   - **`model_vendor_slug` 必填**（只给 name 会 400）。register_hint 已按模型名猜了一个；**向供应商确认**，猜不出(空)就问他属于哪个厂商（openai/anthropic/google/deepseek/qwen/zhipu-ai/xai/midjourney…）。
   - **slug 优先复用平台已有模型**：若平台已有同名/对应模型（如 `gpt-4o`），`slug` 用平台的，让端点挂到正确目录下；**别乱起新 slug**（会新建一个垃圾目录模型）。`name`/`type`/`category` 也尽量对齐平台已有模型。
   - `auth_type` 用探测报告里的 `auth_type.detected`（bearer/header/query）；若探测显示"未能确认"，问供应商其上游用哪种认证。
5. **注册前先查重**：用 `GET /api/v1/provider/models`（带 token）看这个模型是否**已注册且 active**。
   - 已 active → **警告供应商**：重新注册会删旧端点、打回 pending、期间掉线；要改配置应走「变更请求」而非重注册。除非他明确要重建，否则跳过。
   - 不存在或仅 pending/草稿 → 正常继续。
6. **把完整请求体给供应商确认**，确认后 POST（用步骤 1 的 token）：
   ```bash
   curl -sS -X POST https://api.aicoming.top/api/v1/provider/models \
     -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
     -d '<providerModelRequest JSON>'
   ```
7. 记录返回（成功/失败原因），失败按实际响应调字段重试，不硬套。然后进入下一个模型。

### 步骤 6 — 收尾
汇总：哪些已注册（pending，归属其商家）、哪些跳过及原因、需补测/人工的项。明确告诉供应商：
- **下一步等管理员审核，通过才上架。**
- **以后改价/改配置**：不会自动同步（价格无可读数据源）。要改时在控制台、或重跑本 skill 走「变更请求」(`POST /api/v1/provider/change-requests`，type=edit)，管理员审核通过后生效。

## 参考资料

- `references/aicoming-contract.md` —— 接入契约、5 条适配点、图片/edit 规则、兼容判定速查。
- `references/onboard-api.md` —— 登录、注册、变更接口的真实字段、鉴权、状态流、完整 URL 约定。
- `scripts/probe_upstream.py` —— 全量发现 + 逐模型探测（文生图+图生图），输出每模型 verdict + 完整 URL 注册建议。
- `scripts/usd_to_cny.py` —— 美元定价按汇率折算成人民币（4 位小数）。
