"""일봉 백테스트 — 같은 전략을 과거 데이터에 돌려 봅니다 (종가 체결 가정, 수수료/세금 반영).

    python -m autotrader.backtest --code 005930            # 키움 API 로 일봉 조회
    python -m autotrader.backtest --csv prices.csv          # CSV(날짜,종가) 사용
"""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from typing import Optional, Sequence

from .strategy import StrategyConfig, decide

FEE_RATE = 0.00015   # 매매 수수료(편도) 가정
TAX_RATE = 0.0018    # 매도 시 증권거래세 등 가정


@dataclass
class Trade:
    buy_date: str
    buy_price: int
    sell_date: str
    sell_price: int
    qty: int
    reason: str

    @property
    def pnl(self) -> float:
        gross = (self.sell_price - self.buy_price) * self.qty
        cost = (self.buy_price + self.sell_price) * self.qty * FEE_RATE + self.sell_price * self.qty * TAX_RATE
        return gross - cost


def run_backtest(rows: Sequence[tuple[str, int]], cfg: StrategyConfig, budget: int = 1_000_000) -> list[Trade]:
    trades: list[Trade] = []
    pos: Optional[tuple[str, int, int]] = None  # (date, price, qty)
    closes: list[int] = []
    for d, price in rows:
        closes.append(price)
        sig = decide(closes, pos[1] if pos else None, cfg)
        if pos is None and sig.action == "BUY":
            qty = budget // price
            if qty > 0:
                pos = (d, price, qty)
        elif pos is not None and sig.action == "SELL":
            trades.append(Trade(pos[0], pos[1], d, price, pos[2], sig.reason))
            pos = None
    if pos is not None:
        d, price = rows[-1]
        trades.append(Trade(pos[0], pos[1], d, price, pos[2], "기간 종료 청산"))
    return trades


def summarize(trades: list[Trade], budget: int) -> str:
    if not trades:
        return "거래 없음"
    total = sum(t.pnl for t in trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    lines = [f"{t.buy_date}→{t.sell_date}  {t.buy_price:>9,} → {t.sell_price:>9,}  "
             f"{t.pnl:>+12,.0f}원  {t.reason}" for t in trades]
    lines.append("-" * 72)
    lines.append(f"거래 {len(trades)}회, 승률 {wins / len(trades) * 100:.1f}%, "
                 f"총손익 {total:+,.0f}원 (종목당 예산 {budget:,}원 대비 {total / budget * 100:+.2f}%)")
    return "\n".join(lines)


def load_csv(path: str) -> list[tuple[str, int]]:
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if len(r) < 2 or not r[1].replace(",", "").strip().lstrip("-").isdigit():
                continue  # 헤더/빈 줄 건너뜀
            rows.append((r[0].strip(), int(r[1].replace(",", ""))))
    rows.sort()
    return rows


def main() -> None:
    from .trader import PKG_DIR, AppConfig, build_client, load_env

    ap = argparse.ArgumentParser(description="이동평균 전략 백테스트")
    ap.add_argument("--code", help="종목코드 (키움 API 조회)")
    ap.add_argument("--csv", help="날짜,종가 CSV 파일")
    ap.add_argument("--config", default=os.path.join(PKG_DIR, "config.json"))
    args = ap.parse_args()

    cfg = AppConfig.load(args.config)
    if args.csv:
        rows = load_csv(args.csv)
    elif args.code:
        load_env(os.path.join(PKG_DIR, ".env"), os.path.join(os.path.dirname(PKG_DIR), ".env"))
        client = build_client(cfg)
        try:
            rows = client.get_daily_closes(args.code.zfill(6))
        finally:
            client.revoke()
    else:
        ap.error("--code 또는 --csv 를 지정하세요.")
    print(f"데이터 {len(rows)}일 ({rows[0][0]} ~ {rows[-1][0]})" if rows else "데이터 없음")
    print(summarize(run_backtest(rows, cfg.strategy, cfg.risk.budget_per_stock), cfg.risk.budget_per_stock))


if __name__ == "__main__":
    main()
