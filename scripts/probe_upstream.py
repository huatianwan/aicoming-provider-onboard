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
import sys
import urllib.request
import urllib.error


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


def detect_auth(base, key):
    """探测上游接受哪种认证：bearer / header / query。返回 (auth, 依据)。"""
    # 优先用 /models（不花钱）。200 = 该认证被接受。
    for a in ("bearer", "header", "query"):
        st, _ = _req(f"{base}/models", key, auth=a)
        if st == 200:
            return a, f"/models 200 via {a}"
    # /models 不可用 → 各认证打一个极小 chat；非 401/403 视为认证通过。
    for a in ("bearer", "header", "query"):
        st, _ = _req(f"{base}/chat/completions", key, "POST",
                     {"model": "probe", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                     auth=a)
        if st not in (401, 403, 0):
            return a, f"chat {st} via {a}（非 401/403 视为认证通过）"
    return "bearer", "未能确认，默认 bearer（认证方式可能特殊，需向供应商确认）"


def discover_models(base, key):
    status, raw = _req(f"{base}/models", key)
    out = {"status": status, "ok": status == 200, "models": [],
           "supported_types": {}, "raw_head": raw[:200]}
    if status == 200:
        try:
            data = json.loads(raw)
            items = data.get("data", data if isinstance(data, list) else [])
            for m in items:
                if isinstance(m, dict) and m.get("id"):
                    out["models"].append(m["id"])
                    if m.get("supported_endpoint_types"):
                        out["supported_types"][m["id"]] = m["supported_endpoint_types"]
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
    status, raw = _req(f"{base}/embeddings", key, "POST", body)
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
                    {"model": model, "messages": [{"role": "user", "content": "ping"}], field: 16})
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
    status, resp = _req(f"{base}/chat/completions", key, "POST", body, stream=True)
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


def guess_vendor_slug(model):
    """按模型名猜平台 model_vendor_slug（注册必填，留空则需向供应商确认）。"""
    n = (model or "").lower()
    if "claude" in n or "anthropic" in n:
        return "anthropic"
    if "gemini" in n or n.startswith("google"):
        return "google"
    if "deepseek" in n:
        return "deepseek"
    if "qwen" in n or "tongyi" in n:
        return "qwen"
    if "glm" in n or "zhipu" in n:
        return "zhipu-ai"
    if "grok" in n or "xai" in n:
        return "xai"
    if "midjourney" in n:
        return "midjourney"
    if any(k in n for k in ("gpt", "o1", "o3", "o4", "dall", "openai", "image", "whisper", "tts")):
        return "openai"
    return ""  # 猜不出 → skill 向供应商确认


def register_hint(base, model, mtype, auth="bearer"):
    """每个模型的注册建议：提交**完整 URL** + path_prefix=__raw__，不靠平台拼接。"""
    h = {"upstream_model": model, "path_prefix": "__raw__",
         "auth_type": auth, "model_vendor_slug": guess_vendor_slug(model)}
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


def probe_one(base, key, model, mtype, do_image, auth="bearer"):
    r = {"model": model, "model_type": mtype, "models": {"ok": True}}
    if mtype == "chat":
        ns = probe_chat_nonstream(base, key, model)
        r["chat_nonstream"] = ns
        r["chat_stream"] = probe_chat_stream(base, key, model, ns.get("tokens_field", "max_tokens"))
    elif mtype == "image":
        if do_image:
            r["image_gen"] = probe_image_gen(base, key, model)
            r["image_edit"] = probe_image_edit(base, key, model, "image")
            if not r["image_edit"]["ok"]:
                r["image_edit_alt"] = probe_image_edit(base, key, model, "image[]")
        else:
            r["image_gen"] = None
            r["image_edit"] = None
    elif mtype == "embedding":
        r["embeddings"] = probe_embeddings(base, key, model)
    # audio/rerank: 留待人工
    r["assessment"] = assess(r)
    r["register_hint"] = register_hint(base, model, mtype, auth)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="上游 Base URL，到 /v1 为止")
    ap.add_argument("--key", required=True, help="上游 API key")
    ap.add_argument("--model", help="只探测这一个模型；不传则探测 /models 发现的**全部**模型")
    ap.add_argument("--image", action="store_true", help="探测图片模型（文生图+图生图，会产生真实费用）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # 先探测认证方式（bearer/header/query），后续所有请求复用
    auth, auth_basis = detect_auth(base, args.key)
    _AUTH[0] = auth

    disc = discover_models(base, args.key)
    report = {"base_url": base, "auth_type": {"detected": auth, "basis": auth_basis},
              "models": disc, "results": []}

    targets = [args.model] if args.model else disc["models"]
    if not targets:
        report["note"] = "拿不到模型清单（/models 不可用），且未指定 --model。请让供应商提供模型清单后用 --model 逐个探测。"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    for m in targets:
        mtype = classify_model(m, disc.get("supported_types", {}).get(m))
        report["results"].append(probe_one(base, args.key, m, mtype, args.image, auth))

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
