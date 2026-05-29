#!/usr/bin/env python3
"""Probe a supplier's upstream API for AIComing onboarding.

Detects OpenAI compatibility and the specific traits AIComing's relay cares about,
then prints a JSON compatibility report to stdout.

Usage:
    probe_upstream.py --base https://api.x.com/v1 --key sk-xxx [--model gpt-4o] [--image]

Notes:
- Uses only the Python stdlib (urllib) so it runs anywhere.
- Chat probes are cheap (1-token ping). The image probe COSTS money on most
  providers, so it is opt-in via --image.
- Never prints the API key. The report is safe to share.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

# 复用同目录的价格抓取（多家中转：内联 /models 价、new-api /api/pricing、供应商给的 URL）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from fetch_pricing import (fetch_pricing_map, fetch_pricing_from_url,
                               normalize_model_obj_pricing)
except Exception:  # 脚本被单独拷走时不致崩
    fetch_pricing_map = fetch_pricing_from_url = normalize_model_obj_pricing = None


# 检测到的认证方式，被所有请求复用（main 里先 detect_auth 设定）。
_AUTH = ["bearer"]


def _apply_auth(url, key, auth):
    """按认证方式返回 (最终url, 要加的头)。bearer/header/query 三选一。"""
    headers = {}
    if auth == "header":
        headers["X-API-Key"] = key
    elif auth == "query":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={key}"
    else:  # bearer（默认）
        headers["Authorization"] = f"Bearer {key}"
    return url, headers


def _req(url, key, method="GET", body=None, stream=False, timeout=60, auth=None):
    url, headers = _apply_auth(url, key, auth or _AUTH[0])
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        if stream:
            return resp.status, resp  # caller reads lines
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # network/timeout/TLS
        return 0, f"__error__:{type(e).__name__}:{e}"


def resolve_base_auth(base, key):
    """同时探测「有效 base」(是否需要补 /v1) 和「认证方式」(bearer/header/query)。
    不写死 /v1：先试供应商给的 base，再试 base+/v1。返回 (有效base, auth, 依据, models可用)。"""
    base = base.rstrip("/")
    bases = [base]
    if not base.lower().endswith("/v1"):
        bases.append(base + "/v1")
    # 优先用 /models（不花钱）：哪个 base×auth 组合 200，就定了 base 和 auth。
    # 短超时(15s)：检测阶段死端点快速失败，不要 60s 干等。
    for b in bases:
        for a in ("bearer", "header", "query"):
            st, _ = _req(f"{b}/models", key, auth=a, timeout=15)
            if st == 200:
                return b, a, f"/models 200 @ {b} via {a}", True
    # /models 不可用 → 用极小 chat ping 探（非 401/403/404 视为该 base+auth 通）。
    for b in bases:
        for a in ("bearer", "header", "query"):
            st, _ = _req(f"{b}/chat/completions", key, "POST",
                         {"model": "probe", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                         auth=a, timeout=15)
            if st not in (401, 403, 404, 0):
                return b, a, f"chat {st} @ {b} via {a}（非401/403/404视为通）", False
    return base, "bearer", "未能确认 base/auth，默认（需向供应商确认完整端点与认证方式）", False


def discover_models(base, key, rate=7.2):
    status, raw = _req(f"{base}/models", key)
    out = {"status": status, "ok": status == 200, "models": [],
           "supported_types": {}, "owned_by": {}, "inline_pricing": {}, "raw_head": raw[:200]}
    if status == 200:
        try:
            data = json.loads(raw)
            items = data.get("data", data if isinstance(data, list) else [])
            for m in items:
                if isinstance(m, dict) and m.get("id"):
                    out["models"].append(m["id"])
                    if m.get("supported_endpoint_types"):
                        out["supported_types"][m["id"]] = m["supported_endpoint_types"]
                    if m.get("owned_by"):
                        out["owned_by"][m["id"]] = m["owned_by"]  # /models 自带的厂商线索
                    # 价格内联在 models 里（OpenRouter/LiteLLM 等）→ 免令牌直接拿成本
                    if normalize_model_obj_pricing:
                        row = normalize_model_obj_pricing(m, rate)
                        if row:
                            out["inline_pricing"][m["id"]] = row
        except Exception:
            out["ok"] = False
    return out


def classify_model(name, supported_types=None):
    """Guess the model family from its id (+ supported_endpoint_types if present)."""
    types = " ".join(supported_types or []).lower()
    n = (name or "").lower()
    if "image" in types or any(k in n for k in ("image", "dall", "flux", "sd-", "stable-diffusion", "midjourney")):
        return "image"
    if "embedding" in types or "embed" in n or "bge" in n or "rerank" in n:
        return "rerank" if "rerank" in n else "embedding"
    if any(k in n for k in ("whisper", "tts", "audio", "speech")):
        return "audio"
    return "chat"


def probe_embeddings(base, key, model):
    body = {"model": model, "input": "hello"}
    status, raw = _req(f"{base}/embeddings", key, "POST", body, timeout=25)
    r = {"status": status, "ok": status == 200, "has_embedding": False, "raw_head": raw[:200]}
    if status == 200:
        try:
            r["has_embedding"] = bool((json.loads(raw).get("data") or [{}])[0].get("embedding"))
        except Exception:
            r["ok"] = False
    return r


def probe_chat_nonstream(base, key, model):
    def call(field):
        return _req(f"{base}/chat/completions", key, "POST",
                    {"model": model, "messages": [{"role": "user", "content": "ping"}], field: 16},
                    timeout=30)
    tokens_field = "max_tokens"
    status, raw = call(tokens_field)
    # 推理模型(o1/o3 类)拒绝 max_tokens、要 max_completion_tokens → 回退重试
    if status == 400 and "max_completion_tokens" in raw:
        tokens_field = "max_completion_tokens"
        status, raw = call(tokens_field)
    r = {"status": status, "ok": status == 200, "has_choices": False,
         "has_usage": False, "tokens_field": tokens_field, "raw_head": raw[:300]}
    if status == 200:
        try:
            d = json.loads(raw)
            r["has_choices"] = bool(d.get("choices"))
            r["has_usage"] = bool(d.get("usage"))
            r["object"] = d.get("object")
        except Exception:
            r["ok"] = False
    return r


def probe_chat_stream(base, key, model, tokens_field="max_tokens"):
    body = {"model": model, "messages": [{"role": "user", "content": "count to 3"}],
            "stream": True, tokens_field: 20}
    r = {"ok": False, "got_sse": False, "saw_done": False,
         "usage_in_stream": False, "status": 0, "note": ""}
    status, resp = _req(f"{base}/chat/completions", key, "POST", body, stream=True, timeout=30)
    r["status"] = status
    if status != 200 or isinstance(resp, str):
        r["note"] = resp[:200] if isinstance(resp, str) else f"status {status}"
        return r
    last_data = ""
    try:
        for line_b in resp:
            line = line_b.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            r["got_sse"] = True
            payload = line[5:].strip()
            if payload == "[DONE]":
                r["saw_done"] = True
                break
            last_data = payload
            if '"usage"' in payload:
                try:
                    if json.loads(payload).get("usage"):
                        r["usage_in_stream"] = True
                except Exception:
                    pass
    except Exception as e:
        r["note"] = f"stream read error: {e}"
    r["ok"] = r["got_sse"]
    if not r["usage_in_stream"] and last_data and '"usage"' in last_data:
        r["usage_in_stream"] = True
    return r


def probe_image_gen(base, key, model):
    body = {"model": model, "prompt": "a red circle on white", "n": 1, "size": "1024x1024"}
    status, raw = _req(f"{base}/images/generations", key, "POST", body, timeout=180)
    r = {"status": status, "ok": status == 200, "format": None, "raw_head": raw[:200]}
    if status == 200:
        try:
            item = (json.loads(raw).get("data") or [{}])[0]
            r["format"] = "b64_json" if item.get("b64_json") else ("url" if item.get("url") else "unknown")
        except Exception:
            r["ok"] = False
    return r


def _make_test_png(w=512, h=512, rgb=(180, 140, 90)):
    """Generate a valid solid-color PNG with stdlib only (no Pillow)."""
    import zlib, struct
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = (b"\x00" + bytes(rgb) * w) * h
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _multipart(fields, files):
    """Build a multipart/form-data body. fields: dict; files: [(name, filename, bytes)]."""
    boundary = "----aicomingprobe7f3a9c2e"
    out = []
    for name, val in fields.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
    for name, fn, data in files:
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fn}\"\r\n".encode()
                   + b"Content-Type: image/png\r\n\r\n" + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _post_multipart(url, key, body, content_type, timeout=180):
    url, headers = _apply_auth(url, key, _AUTH[0])
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", content_type)
    req.add_header("Accept", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"__error__:{type(e).__name__}:{e}"


def probe_image_edit(base, key, model, field_name="image"):
    """图生图：用平台实际转发的 multipart 形态打 /images/edits（字段名默认 image）。"""
    png = _make_test_png()
    fields = {"model": model, "prompt": "make it a smooth gradient", "size": "1024x1024"}
    body, ct = _multipart(fields, [(field_name, "ref.png", png)])
    status, raw = _post_multipart(f"{base}/images/edits", key, body, ct)
    r = {"field": field_name, "status": status, "ok": status == 200,
         "format": None, "raw_head": raw[:200]}
    if status == 200:
        try:
            item = (json.loads(raw).get("data") or [{}])[0]
            r["format"] = "b64_json" if item.get("b64_json") else ("url" if item.get("url") else "unknown")
        except Exception:
            r["ok"] = False
    return r


def assess(report):
    """Turn raw probe data into an AIComing-compatibility verdict, per model type."""
    issues = []
    mtype = report.get("model_type", "chat")
    if not report.get("models", {}).get("ok"):
        issues.append("GET /models 不可用：上架时需手动提供模型清单（不阻断）")

    if mtype == "chat":
        chat = report.get("chat_nonstream", {})
        stream = report.get("chat_stream", {})
        if not chat.get("ok"):
            issues.append(f"/chat/completions 非流式失败(status {chat.get('status')})：路径/认证/格式不兼容")
        elif not chat.get("has_choices"):
            issues.append("非流式 200 但响应不是 OpenAI 结构(无 choices)——疑似原生协议(如 Gemini 的 candidates / Anthropic 的 content)。"
                          "平台会翻译请求体，但**不回译响应**，OpenAI 客户端会拿到非标准响应 → 不兼容。需上游提供 OpenAI 格式的响应入口。")
        elif not chat.get("has_usage"):
            issues.append("非流式响应缺 usage：平台将估算 token，计费可能不准（不阻断）")
        if stream.get("ok") and not stream.get("usage_in_stream"):
            issues.append("流式最后一个 chunk 缺 usage：平台将估算 token（适配点②，不阻断）")
        if not stream.get("ok"):
            issues.append("流式不可用或非标准 SSE：流式请求可能失败")
        # 兼容 = OpenAI 格式请求成功 且 响应是 OpenAI 结构（按完整 URL 实测判定，不看模型名）
        core_ok = bool(chat.get("ok") and chat.get("has_choices") and stream.get("ok"))
    elif mtype == "image":
        gen = report.get("image_gen")
        edit = report.get("image_edit")
        if gen is None:
            issues.append("图片模型未实测（探测时未加 --image，会产生真实生图费用）：必须加 --image 复跑确认文生图+图生图")
            core_ok = None
        else:
            # 文生图
            if not gen.get("ok"):
                issues.append(f"文生图 /images/generations 失败(status {gen.get('status')})：路径/认证/格式不兼容")
            elif gen.get("format") == "url":
                issues.append("图片返回 url（非 b64_json）：平台透传，客户端需兼容 url（不阻断）")
            # 图生图
            edit_ok = bool(edit and edit.get("ok"))
            if edit and not edit.get("ok"):
                alt = report.get("image_edit_alt")
                if alt and alt.get("ok"):
                    issues.append("图生图：仅接受 image[] 字段（不收 image）。平台两种都转发，客户端用 image[] 即可（不阻断）")
                    edit_ok = True
                else:
                    issues.append(f"图生图 /images/edits 失败(status {edit.get('status')})：edits 路径不可达或字段/格式不兼容——"
                                  "若 edits 不在 generations 同级，需管理员补 upstream_edit_url；字段/协议不同则当前无法自动适配")
            gen_ok = bool(gen.get("ok"))
            if gen_ok and edit_ok:
                core_ok = True
            elif gen_ok and not edit_ok:
                issues.append("⚠️ 仅文生图可用、图生图未通过：只能作为'纯文生图'端点上架，或先解决图生图再上")
                core_ok = "partial"
            else:
                core_ok = False
    elif mtype == "embedding":
        emb = report.get("embeddings", {})
        if not emb.get("ok"):
            issues.append(f"/embeddings 失败(status {emb.get('status')})：路径/认证/格式不兼容")
        core_ok = bool(emb.get("ok"))
    else:  # audio / rerank / unknown
        issues.append(f"模型类型={mtype}：本脚本暂不自动探测，请手动验证对应接口")
        core_ok = None

    if core_ok == "partial":
        verdict = "needs_attention"
    elif core_ok is True:
        blocking = [i for i in issues if "失败" in i or "不可用或非标准" in i]
        verdict = "compatible" if not blocking else "needs_attention"
    elif core_ok is False:
        verdict = "unusable"
    else:
        verdict = "needs_manual_check"
    return {"model_type": mtype, "verdict": verdict, "issues": issues}


# 平台已知厂商 slug（model_vendors 表里真实可选的，owned_by 兜底匹配用）。
_VENDOR_SLUGS = ("openai", "anthropic", "google", "deepseek", "qwen", "zhipu-ai",
                 "xai", "midjourney", "minimax", "baidu-qianfan", "alibaba-bailian",
                 "iflytek-spark", "tencent-hunyuan", "ollama", "kling-ai",
                 "moonshot", "stepfun", "mistral", "meta", "cohere", "perplexity",
                 "baichuan", "nvidia", "microsoft", "yi", "volcano-engine",
                 "openrouter", "siliconflow", "together", "groq", "fireworks")

# OpenRouter / 聚合站常用 "vendor/model" 前缀 → 平台 slug（前缀是最强的厂商信号）。
_PREFIX_SLUGS = {
    "openai": "openai", "anthropic": "anthropic", "google": "google",
    "deepseek": "deepseek", "qwen": "qwen", "alibaba": "qwen",
    "x-ai": "xai", "xai": "xai", "mistralai": "mistral", "mistral": "mistral",
    "meta-llama": "meta", "meta": "meta", "moonshotai": "moonshot", "moonshot": "moonshot",
    "stepfun": "stepfun", "stepfun-ai": "stepfun", "cohere": "cohere",
    "perplexity": "perplexity", "nvidia": "nvidia", "microsoft": "microsoft",
    "z-ai": "zhipu-ai", "zhipuai": "zhipu-ai", "zhipu": "zhipu-ai", "thudm": "zhipu-ai",
    "minimax": "minimax", "minimaxai": "minimax", "01-ai": "yi", "baichuan": "baichuan",
    "baidu": "baidu-qianfan", "bytedance": "volcano-engine", "tencent": "tencent-hunyuan",
}


def _vendor_from_name(n):
    if "claude" in n or "anthropic" in n:
        return "anthropic"
    if "gemini" in n or "imagen" in n or "gemma" in n or n.startswith("google"):
        return "google"
    if "deepseek" in n:
        return "deepseek"
    if "qwen" in n or "tongyi" in n or "通义" in n:
        return "qwen"
    if "glm" in n or "zhipu" in n or "智谱" in n or "chatglm" in n:
        return "zhipu-ai"
    if "grok" in n or "xai" in n:
        return "xai"
    if "midjourney" in n or n.startswith("mj-"):
        return "midjourney"
    if "minimax" in n or "abab" in n:
        return "minimax"
    if "ernie" in n or "wenxin" in n or "文心" in n or "qianfan" in n:
        return "baidu-qianfan"
    if "hunyuan" in n or "混元" in n:
        return "tencent-hunyuan"
    if "spark" in n or "星火" in n:
        return "iflytek-spark"
    if "kling" in n or "可灵" in n:
        return "kling-ai"
    if "kimi" in n or "moonshot" in n:
        return "moonshot"
    if "step-" in n or n.startswith("step"):
        return "stepfun"
    if "mistral" in n or "mixtral" in n or "codestral" in n:
        return "mistral"
    if "llama" in n or n.startswith("meta"):
        return "meta"
    if "command" in n or "cohere" in n:
        return "cohere"
    if "sonar" in n or "perplexity" in n:
        return "perplexity"
    if "baichuan" in n:
        return "baichuan"
    if "doubao" in n or "豆包" in n:
        return "volcano-engine"
    if any(k in n for k in ("gpt", "o1-", "o3", "o4", "dall", "openai", "whisper",
                            "tts", "text-embedding", "codex", "image")):
        return "openai"
    return ""


def resolve_vendor_slug(model, owned_by=""):
    """多信号自动分辨平台 model_vendor_slug，返回 (slug, 依据)。
    顺序：vendor/ 前缀（最强）→ 模型名 → owned_by 兜底。中转常把 owned_by 一律设成 openai，故名字/前缀优先。"""
    m = (model or "").lower().strip()
    # 1) "vendor/model" 前缀（OpenRouter、多数聚合站）——最可靠
    if "/" in m:
        prefix = m.split("/", 1)[0]
        if prefix in _PREFIX_SLUGS:
            return _PREFIX_SLUGS[prefix], f"按 vendor/ 前缀（{prefix}）"
        by = _vendor_from_name(prefix)
        if by:
            return by, f"按 vendor/ 前缀（{prefix}）"
    # 2) 模型名
    byname = _vendor_from_name(m)
    if byname:
        return byname, "按模型名"
    # 3) owned_by 兜底
    ob = (owned_by or "").lower().strip()
    if ob:
        for s in _VENDOR_SLUGS:
            head = s.split("-")[0]
            if ob == s or ob == s.replace("-", "") or (len(head) > 3 and head in ob):
                return s, f"owned_by={owned_by}"
    return "", "无法自动分辨，需向供应商确认"


def register_hint(base, model, mtype, auth="bearer", owned_by=""):
    """每个模型的注册建议：提交**完整 URL** + path_prefix=__raw__，不靠平台拼接。"""
    vslug, vbasis = resolve_vendor_slug(model, owned_by)
    h = {"upstream_model": model, "path_prefix": "__raw__",
         "auth_type": auth, "model_vendor_slug": vslug, "vendor_basis": vbasis}
    if mtype == "chat":
        h["upstream_url"] = base + "/chat/completions"
        h["billing_type"] = "token"
    elif mtype == "image":
        h["upstream_url"] = base + "/images/generations"
        h["upstream_edit_url"] = base + "/images/edits"
        h["upstream_edit_model"] = model
        h["billing_type"] = "image"
    elif mtype == "embedding":
        h["upstream_url"] = base + "/embeddings"
        h["billing_type"] = "token"
    else:
        h["upstream_url"] = base  # 未知类型，待人工确认接口
    return h


def attach_pricing(results, pmap, markup):
    """把抓到的上游成本并进每个模型的 register_hint，并按加价率给出建议售价。
    pmap: {model_name -> 成本行}; markup: 如 0.2 表示加价 20%（None=不建议售价）。"""
    # 大小写不敏感的成本查找。
    low = {k.lower(): v for k, v in pmap.items()}
    mult = (1 + markup) if markup is not None else None
    for r in results:
        h = r.get("register_hint", {})
        cost = pmap.get(r["model"]) or low.get(r["model"].lower())
        if not cost:
            h["pricing_basis"] = "未抓到该模型成本，请向供应商手填"
            continue
        h["pricing_basis"] = "来自自动抓取"
        ic, oc = cost.get("upstream_input_cost"), cost.get("upstream_output_cost")
        cc = cost.get("call_cost_cny")
        if ic is not None:  # token 计费
            h["upstream_input_cost"] = ic
            h["upstream_output_cost"] = oc
            if mult is not None:
                h["suggested_input_price"] = round(ic * mult, 4)
                h["suggested_output_price"] = round((oc or 0) * mult, 4)
        elif cc is not None:  # 按次/图片：上游成本无对应存储字段，仅给建议售价
            h["upstream_call_cost_note"] = f"上游约 ¥{cc}/次，注册接口无 upstream call cost 字段，不入库"
            if mult is not None:
                key = "suggested_image_price" if h.get("billing_type") == "image" else "suggested_call_price"
                h[key] = round(cc * mult, 4)
    return results


def probe_one(base, key, model, mtype, do_image, auth="bearer", owned_by=""):
    r = {"model": model, "model_type": mtype, "models": {"ok": True}}
    if mtype == "chat":
        ns = probe_chat_nonstream(base, key, model)
        r["chat_nonstream"] = ns
        r["chat_stream"] = probe_chat_stream(base, key, model, ns.get("tokens_field", "max_tokens"))
    elif mtype == "image":
        if do_image:
            # 文生图 + 图生图并行（两个独立端点，省一半等待）
            with ThreadPoolExecutor(max_workers=2) as ex:
                fg = ex.submit(probe_image_gen, base, key, model)
                fe = ex.submit(probe_image_edit, base, key, model, "image")
                r["image_gen"] = fg.result()
                r["image_edit"] = fe.result()
            if not r["image_edit"]["ok"]:
                r["image_edit_alt"] = probe_image_edit(base, key, model, "image[]")
        else:
            r["image_gen"] = None
            r["image_edit"] = None
    elif mtype == "embedding":
        r["embeddings"] = probe_embeddings(base, key, model)
    # audio/rerank: 留待人工
    r["assessment"] = assess(r)
    r["register_hint"] = register_hint(base, model, mtype, auth, owned_by)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="上游 Base URL（host 或到 /v1 都行，脚本自动判断是否补 /v1）")
    ap.add_argument("--key", required=True, help="上游 API key")
    ap.add_argument("--model", help="只探测这一个模型；不传则探测 /models 发现的**全部**模型")
    ap.add_argument("--image", action="store_true", help="探测图片模型（文生图+图生图，会产生真实费用）")
    ap.add_argument("--concurrency", type=int, default=5, help="并发探测的模型数（默认5；上游限流严就调小到1-2）")
    ap.add_argument("--pricing", action="store_true", help="同时抓上游成本（中转 /api/pricing），并进每个模型的注册建议")
    ap.add_argument("--pricing-token", default="", help="中转「系统访问令牌」（非 sk- key）；公开 pricing 可不填")
    ap.add_argument("--pricing-url", default="", help="兜底：供应商给的单个价目 URL（价格页接口/models 接口），自动判形态")
    ap.add_argument("--markup", type=float, help="加价率，如 0.2=加价20%%；给了就按成本自动算建议售价")
    ap.add_argument("--rate", type=float, default=7.2, help="USD->CNY 汇率（中转价基本按美元，默认7.2）")
    args = ap.parse_args()
    raw_base = args.base.rstrip("/")

    # 自动探测有效 base(是否补/v1) + 认证方式，后续所有请求复用有效 base。
    base, auth, basis, _ = resolve_base_auth(raw_base, args.key)
    _AUTH[0] = auth

    disc = discover_models(base, args.key, args.rate)
    report = {"input_base": raw_base, "effective_base": base,
              "auth_type": {"detected": auth, "basis": basis},
              "models": disc, "results": []}

    targets = [args.model] if args.model else disc["models"]
    if not targets:
        report["note"] = ("拿不到模型清单（该上游无 /models，如 subrouter.ai）。"
                          "请让供应商直接提供模型清单，再用 --model 逐个探测。不要去爬网站。")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    def _probe(m):
        mtype = classify_model(m, disc.get("supported_types", {}).get(m))
        owned_by = disc.get("owned_by", {}).get(m, "")
        return probe_one(base, args.key, m, mtype, args.image, auth, owned_by)

    # 并发探测多个模型（默认5路），保持输出顺序与 targets 一致。
    workers = max(1, min(args.concurrency, len(targets)))
    if workers == 1:
        report["results"] = [_probe(m) for m in targets]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            report["results"] = list(ex.map(_probe, targets))

    # 抓上游成本并算建议售价：多源合并（① 内联 /models 价 → ② new-api /api/pricing → ③ 供应商给的 URL）
    if args.pricing:
        if fetch_pricing_map is None:
            report["pricing"] = {"source": None, "note": "fetch_pricing 模块不可用（脚本被单独拷走？）"}
        else:
            pmap = {}
            sources = []
            # ① 内联在 models 里的价格（OpenRouter/LiteLLM 等，免令牌）
            inline = disc.get("inline_pricing", {})
            if inline:
                pmap.update(inline)
                sources.append(f"内联/models({len(inline)})")
            # ② new-api /api/pricing（比率制，可能需令牌）—— 覆盖内联
            np, meta = fetch_pricing_map(raw_base, args.pricing_token, args.rate)
            if np:
                pmap.update(np)
                sources.append(f"/api/pricing({len(np)})")
            note = meta.get("note", "")
            # ③ 供应商给的单个价目 URL —— 最高优先级覆盖
            if args.pricing_url:
                up, m3 = fetch_pricing_from_url(args.pricing_url, args.pricing_token, args.rate)
                if up:
                    pmap.update(up)
                    sources.append(f"URL({len(up)})")
                if m3.get("note"):
                    note = (note + " | " if note else "") + m3["note"]
            attach_pricing(report["results"], pmap, args.markup)
            report["pricing"] = {"sources": sources or ["（无）"], "note": note,
                                 "matched": sum(1 for r in report["results"]
                                                if r["register_hint"].get("pricing_basis", "").startswith("来自")),
                                 "rate": args.rate, "markup": args.markup}

    # 汇总
    report["summary"] = {
        "total": len(report["results"]),
        "compatible": sum(1 for r in report["results"] if r["assessment"]["verdict"] == "compatible"),
        "needs_attention": sum(1 for r in report["results"] if r["assessment"]["verdict"] == "needs_attention"),
        "unusable": sum(1 for r in report["results"] if r["assessment"]["verdict"] == "unusable"),
        "needs_manual_check": sum(1 for r in report["results"] if r["assessment"]["verdict"] in ("needs_manual_check", "needs_image_probe")),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
