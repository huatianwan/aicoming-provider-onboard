#!/usr/bin/env python3
"""尽力而为地从中转站抓取模型价格（作为「上游成本」建议）。

大多数中转基于 new-api / one-api，暴露 `/api/pricing`：
- 部分部署公开（无需鉴权）→ 直接抓。
- 多数（如 ccapi）需要**中转的「系统访问令牌」**（在中转控制台「个人设置 → 生成系统访问令牌」，
  **不是** sk- 开头的 API key）→ 用 --token 传入，作为 Bearer 调 /api/pricing。
- 根域名常套 Cloudflare（爬不动）；pricing 接口往往就在 **API 网关域名**上（如
  api-direct.ccapi.us/api/pricing），所以这里同时试 API 域名和根域名。

new-api 比率换算（约定）：ratio 1 = $2 / 1M tokens。
  输入 $/1M  = model_ratio * 2
  输出 $/1M  = model_ratio * completion_ratio * 2
  按次计费   = model_price（每次 $）
本脚本把这些换成人民币（--rate），仅作**成本建议**；售价由供应商自定，务必人工确认。

既可当 CLI 单独跑，也被 probe_upstream.py 复用（fetch_pricing_map）。

用法:
    fetch_pricing.py --base https://api-direct.ccapi.us/v1 [--token <系统访问令牌>] [--rate 7.2] [--model <名>]
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


def fetch_pricing_map(base, token, rate):
    """probe_upstream 复用入口。返回 (model->成本行 dict, meta)。"""
    source, data = fetch_raw_pricing(base, token)
    if not source:
        return {}, {"source": None, "note": data.get("note", "")}
    rows = parse_newapi_pricing(data, rate)
    return {r["model"]: r for r in rows}, {"source": source, "note": "", "count": len(rows)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="上游 base 或中转 host（如 https://api-direct.ccapi.us/v1）")
    ap.add_argument("--token", default="", help="中转「系统访问令牌」（非 sk- API key）；公开 pricing 可不填")
    ap.add_argument("--rate", type=float, default=7.2, help="USD->CNY 汇率（new-api 价基本是美元）")
    ap.add_argument("--model", help="只看这一个模型")
    args = ap.parse_args()

    pmap, meta = fetch_pricing_map(args.base, args.token, args.rate)
    result = {"base": args.base, "rate": args.rate, "source": meta.get("source"),
              "note": meta.get("note", ""), "pricing": list(pmap.values())}
    if args.model:
        result["pricing"] = [p for p in result["pricing"] if p["model"] == args.model]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
