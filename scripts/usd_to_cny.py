#!/usr/bin/env python3
"""把供应商的美元定价折算成人民币（AIComing 按 CNY 结算）。

平台所有价格/成本字段单位是人民币（¥/1M token 或 ¥/张），decimal(12,4)。
供应商若用 USD 报价，注册前用本脚本按汇率折算成 CNY，保留 4 位小数。

用法:
    usd_to_cny.py --rate 7.2 '{"input_price":2.0,"output_price":8.0,"image_price":0.04}'

输出 JSON：汇率、逐项 USD→CNY 对照、以及折算后的完整字段（result）。
汇率必须显式传入（涉及钱，不内置默认值，由人确认当日汇率）。
"""
import argparse
import json

# 仅折算这些价格/成本字段；其它字段（模型名、探测结果等）原样保留。
PRICE_FIELDS = {
    "input_price", "output_price", "cache_price",
    "cache_create_5m_price", "cache_create_1h_price",
    "image_price", "call_price",
    "upstream_input_cost", "upstream_output_cost", "upstream_cache_cost",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, required=True, help="USD->CNY 汇率，如 7.2（用当日汇率，需人工确认）")
    ap.add_argument("payload", help="价格字段的 JSON（金额为 USD）")
    args = ap.parse_args()
    if args.rate <= 0:
        raise SystemExit("rate 必须 > 0")

    data = json.loads(args.payload)
    result = dict(data)
    converted = {}
    for k, v in data.items():
        if k in PRICE_FIELDS and isinstance(v, (int, float)):
            cny = round(float(v) * args.rate, 4)
            result[k] = cny
            converted[k] = {"usd": v, "cny": cny}

    print(json.dumps({"rate": args.rate, "converted": converted, "result": result},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
