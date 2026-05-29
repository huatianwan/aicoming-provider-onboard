#!/usr/bin/env python3
"""尽力而为地从中转站抓取模型价格（作为「上游成本」建议），兼容多家平台。

价格在中转上主要有三种形态，本脚本都认：
1. **内联在 /v1/models 里**（OpenRouter、LiteLLM、自建网关）：模型对象自带 `pricing.prompt/completion`
   （$/token）或 `input_cost_per_token` 等。**免令牌、最通用**——probe 抓 models 时顺手就拿到。
2. **new-api / one-api 的 /api/pricing**（比率制）：公开部署直接抓；多数（如 ccapi）需中转的
   **「系统访问令牌」**（控制台「个人设置 → 生成系统访问令牌」，**不是** sk- API key）→ --token 传。
   根域名常套 Cloudflare（爬不动），pricing 接口往往在 **API 网关域名**上，故同时试 API 域名和根域名。
3. **供应商给的单个价目 URL**（--url）：抓那一个、自动判是上面哪种形态。

new-api 比率换算（约定）：ratio 1 = $2 / 1M tokens。
  输入 $/1M = model_ratio*2，输出 $/1M = model_ratio*completion_ratio*2，按次 = model_price（$/次）
全部换成人民币（--rate），仅作**成本建议**；售价由供应商自定，务必人工确认。

既可当 CLI 单独跑，也被 probe_upstream.py 复用（fetch_pricing_map / fetch_pricing_from_url）。

用法:
    fetch_pricing.py --base https://api-direct.ccapi.us/v1 [--token <系统访问令牌>] [--rate 7.2] [--model <名>]
    fetch_pricing.py --url https://reseller.example.com/api/pricing [--token ...]
"""
import argparse
import json
import urllib.parse
import urllib.request
import urllib.error


def _get(url, token):
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)  # new-api 管理接口用 Bearer <系统访问令牌>
    req.add_header("Accept", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"__error__:{type(e).__name__}:{e}"


def guess_pricing_hosts(base):
    """从上游 base 猜可能挂着 /api/pricing 的 host。
    先试 base 自身的 host（pricing 常就在 API 网关域名上），再试根域名。"""
    p = urllib.parse.urlsplit(base if "://" in base else "https://" + base)
    host = p.netloc
    scheme = p.scheme or "https"
    hosts = [f"{scheme}://{host}"]
    labels = host.split(".")
    if len(labels) >= 3:  # api-direct.ccapi.us -> ccapi.us（去掉最前一段子域）
        root = ".".join(labels[1:])
        for h in (f"{scheme}://{root}", f"{scheme}://www.{root}"):
            if h not in hosts:
                hosts.append(h)
    return hosts


def _needs_login(raw):
    low = raw.lower()
    return any(k in low for k in ("not logged in", "access token", "unauthorized", "登录", "令牌"))


def fetch_raw_pricing(base, token):
    """在猜出的 host 上找一个返回 new-api pricing 的端点。
    返回 (source_url, data_dict) 或 (None, {"note": ...})。"""
    last_note = ""
    for host in guess_pricing_hosts(base):
        for path in ("/api/pricing", "/api/ratio_config"):
            status, raw = _get(host + path, token)
            if status == 200:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("success") is False:
                    if _needs_login(raw):
                        last_note = ("中转 %s 需令牌。请供应商在其中转控制台「个人设置 → 生成系统访问令牌」"
                                     "复制一个**系统访问令牌**（不是 sk- API key），用 --token 传入重试；"
                                     "或直接口头报上游成本。" % (host + path))
                    continue
                return host + path, data
            if status in (401, 403) and _needs_login(raw):
                last_note = ("中转 %s 需令牌（HTTP %d）。请供应商提供**系统访问令牌**（--token），或手动报成本。"
                             % (host + path, status))
    return None, {"note": last_note or "未找到可解析的 /api/pricing；该中转可能不是 new-api/one-api，或被 Cloudflare 拦。请手动向供应商要成本。"}


def _to_cny_1m_from_per_token(v, rate):
    """美元/token（OpenRouter/LiteLLM 形态，常为字符串）→ 人民币/百万token。"""
    try:
        return round(float(v) * 1_000_000 * rate, 4)
    except (TypeError, ValueError):
        return None


def normalize_model_obj_pricing(m, rate):
    """从一个 /v1/models 模型对象里抽价格，兼容多家形态，统一成 ¥/1M（或按次）。
    支持：OpenRouter(pricing.prompt/completion，$/token)、LiteLLM(input_cost_per_token…)、
    通用 per-million(input_cost_per_million / prompt_price …)。抽不到返回 None。"""
    if not isinstance(m, dict):
        return None
    row = {}
    p = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
    info = m.get("model_info") if isinstance(m.get("model_info"), dict) else m

    # 1) OpenRouter: pricing.prompt / completion（$/token，字符串）
    if p.get("prompt") is not None or p.get("completion") is not None:
        ic = _to_cny_1m_from_per_token(p.get("prompt"), rate)
        oc = _to_cny_1m_from_per_token(p.get("completion"), rate)
        if ic is not None:
            row["upstream_input_cost"] = ic
            row["upstream_output_cost"] = oc if oc is not None else ic
            cr = _to_cny_1m_from_per_token(p.get("input_cache_read"), rate)
            if cr is not None:
                row["upstream_cache_cost"] = cr
            return row
    # 2) LiteLLM / 通用 per-token：input_cost_per_token / output_cost_per_token
    if info.get("input_cost_per_token") is not None:
        ic = _to_cny_1m_from_per_token(info.get("input_cost_per_token"), rate)
        oc = _to_cny_1m_from_per_token(info.get("output_cost_per_token"), rate)
        if ic is not None:
            row["upstream_input_cost"] = ic
            row["upstream_output_cost"] = oc if oc is not None else ic
            return row
    # 3) 通用 per-million：input_cost_per_million / prompt_price / input_price
    for ik, ok in (("input_cost_per_million", "output_cost_per_million"),
                   ("prompt_price", "completion_price"),
                   ("input_price", "output_price")):
        if info.get(ik) is not None:
            try:
                ic = round(float(info[ik]) * rate, 4)
                oc = round(float(info.get(ok, info[ik])) * rate, 4)
            except (TypeError, ValueError):
                continue
            row["upstream_input_cost"] = ic
            row["upstream_output_cost"] = oc
            return row
    return None


def parse_models_list_pricing(data, rate):
    """解析一个 /v1/models 风格的列表（OpenRouter/LiteLLM/自建），返回 {model_id: 成本行}。"""
    items = data.get("data", data if isinstance(data, list) else [])
    out = {}
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("model_name") or m.get("model")
        if not mid:
            continue
        row = normalize_model_obj_pricing(m, rate)
        if row:
            row["model"] = mid
            out[mid] = row
    return out


def parse_newapi_pricing(data, rate):
    """解析 new-api /api/pricing 的 data[]，换成人民币成本建议。"""
    items = data.get("data", data if isinstance(data, list) else [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("model_name") or it.get("model")
        if not name:
            continue
        mr = it.get("model_ratio")
        cr = it.get("completion_ratio", 1) or 1
        mp = it.get("model_price", 0) or 0
        quota_type = it.get("quota_type", 0)
        row = {"model": name, "quota_type": quota_type}
        if quota_type == 1 or (mp and not mr):  # 按次
            row["call_cost_cny"] = round(float(mp) * rate, 4)
        elif mr is not None:  # 按 token
            in_usd = float(mr) * 2.0
            out_usd = float(mr) * float(cr) * 2.0
            row["upstream_input_cost"] = round(in_usd * rate, 4)   # ¥/1M
            row["upstream_output_cost"] = round(out_usd * rate, 4)  # ¥/1M
        else:
            row["note"] = "无可解析的价格字段"
        out.append(row)
    return out


def _parse_any_pricing(data, rate):
    """对一坨 JSON，先按 new-api /api/pricing（比率）解，没有再按 /v1/models（内联价）解。"""
    rows = parse_newapi_pricing(data, rate)
    pmap = {r["model"]: r for r in rows if "model" in r and ("upstream_input_cost" in r or "call_cost_cny" in r)}
    if pmap:
        return pmap
    return parse_models_list_pricing(data, rate)


def fetch_pricing_map(base, token, rate):
    """probe_upstream 复用入口（new-api /api/pricing）。返回 (model->成本行 dict, meta)。"""
    source, data = fetch_raw_pricing(base, token)
    if not source:
        return {}, {"source": None, "note": data.get("note", "")}
    pmap = _parse_any_pricing(data, rate)
    return pmap, {"source": source, "note": "", "count": len(pmap)}


def fetch_pricing_from_url(url, token, rate):
    """兜底：供应商给的**单个**价目 URL（价格页接口/models 接口），抓那一个、自动判形态。"""
    status, raw = _get(url, token)
    if status != 200:
        return {}, {"source": None, "note": f"价目 URL 返回 HTTP {status}（可能需令牌或地址不对）"}
    try:
        data = json.loads(raw)
    except Exception:
        return {}, {"source": None, "note": "价目 URL 返回的不是 JSON，无法自动解析，请改为手报或粘贴价目"}
    pmap = _parse_any_pricing(data, rate)
    return pmap, {"source": url, "note": "" if pmap else "URL 抓到了但没识别出价格字段，请手报或粘贴价目", "count": len(pmap)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="上游 base 或中转 host（如 https://api-direct.ccapi.us/v1）")
    ap.add_argument("--url", help="兜底：供应商给的单个价目 URL（价格页接口 / models 接口），抓这一个自动判形态")
    ap.add_argument("--token", default="", help="中转「系统访问令牌」（非 sk- API key）；公开 pricing 可不填")
    ap.add_argument("--rate", type=float, default=7.2, help="USD->CNY 汇率（中转价基本是美元）")
    ap.add_argument("--model", help="只看这一个模型")
    args = ap.parse_args()
    if not args.base and not args.url:
        ap.error("至少给 --base 或 --url 之一")

    if args.url:
        pmap, meta = fetch_pricing_from_url(args.url, args.token, args.rate)
    else:
        pmap, meta = fetch_pricing_map(args.base, args.token, args.rate)
    result = {"base": args.base, "url": args.url, "rate": args.rate, "source": meta.get("source"),
              "note": meta.get("note", ""), "pricing": list(pmap.values())}
    if args.model:
        result["pricing"] = [p for p in result["pricing"] if p["model"] == args.model]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
