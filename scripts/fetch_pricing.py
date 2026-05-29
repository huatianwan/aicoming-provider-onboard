#!/usr/bin/env python3
"""尽力而为地从中转站抓取模型价格（作为「上游成本」建议）。

大多数中转基于 new-api / one-api，暴露 `/api/pricing`：
- 部分部署公开（无需鉴权）→ 直接抓。
- 多数（如 ccapi）需要**中转网站的登录态**（cookie/token，**不是** OpenAI API key）
  → 需供应商从其中转控制台提供 access token，或改为手动报价。

new-api 比率换算（约定）：ratio 1 = $2 / 1M tokens。
  输入 $/1M  = model_ratio * 2
  输出 $/1M  = model_ratio * completion_ratio * 2
  按次计费   = model_price（每次 $）
本脚本把这些换成人民币（--rate），仅作**成本建议**；售价由供应商自定，务必人工确认。

用法:
    fetch_pricing.py --base https://中转host [--token <中转web token>] [--rate 7.2] [--model <名>]
"""
import argparse
import json
import urllib.request
import urllib.error


def _get(url, token):
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)  # new-api 管理接口用 Bearer <web token>
    req.add_header("Accept", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"__error__:{type(e).__name__}:{e}"


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="中转 host（如 https://api-direct.ccapi.us 或 https://ccapi.us）")
    ap.add_argument("--token", default="", help="中转网站登录 token（非 API key）；公开 pricing 可不填")
    ap.add_argument("--rate", type=float, default=7.2, help="USD->CNY 汇率（new-api 价基本是美元）")
    ap.add_argument("--model", help="只看这一个模型")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    result = {"base": base, "rate": args.rate, "source": None, "pricing": [], "note": ""}
    # 试几个常见 pricing 路径
    for path in ("/api/pricing", "/api/pricing?"):
        status, raw = _get(base + path, args.token)
        if status == 200:
            try:
                data = json.loads(raw)
                if data.get("success") is False:
                    continue
                result["source"] = base + path
                result["pricing"] = parse_newapi_pricing(data, args.rate)
                break
            except Exception:
                continue
        elif status in (401, 403):
            result["note"] = ("中转 /api/pricing 需登录（返回 %d）。请供应商从其中转控制台提供网站登录 token "
                              "（不是 API key），或直接手动报价。" % status)
    if not result["pricing"] and not result["note"]:
        result["note"] = "未找到可解析的 /api/pricing；该中转可能不是 new-api/one-api，或路径不同。请手动向供应商要价。"
    if args.model and result["pricing"]:
        result["pricing"] = [p for p in result["pricing"] if p["model"] == args.model]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
