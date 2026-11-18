"""예시 전략: 이동평균 골든크로스 매수 + 익절/손절/데드크로스 매도.

- 매수: 단기 이평(기본 5일)이 장기 이평(기본 20일)을 아래에서 위로 돌파 (골든크로스)
- 매도: ① 수익률 >= 익절% ② 수익률 <= -손절% ③ 데드크로스 (옵션)

closes 의 마지막 값에는 '오늘 현재가'를 넣어 장중에 판단합니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class StrategyConfig:
    short_window: int = 5
    long_window: int = 20
    take_profit_pct: float = 3.0
    stop_loss_pct: float = 2.0
    sell_on_dead_cross: bool = True

    def __post_init__(self) -> None:
        if not (1 <= self.short_window < self.long_window):
            raise ValueError("short_window 는 1 이상, long_window 보다 작아야 합니다.")
        if self.take_profit_pct <= 0 or self.stop_loss_pct <= 0:
            raise ValueError("익절/손절 % 는 0보다 커야 합니다.")


@dataclass
class Signal:
    action: str  # "BUY" | "SELL" | "HOLD"
    reason: str


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def cross(closes: Sequence[float], short: int, long: int) -> Optional[str]:
    """직전 봉 대비 이평 교차 여부: 'golden' | 'dead' | None."""
    if len(closes) < long + 1:
        return None
    prev, cur = closes[:-1], closes
    ps, pl = sma(prev, short), sma(prev, long)
    cs, cl = sma(cur, short), sma(cur, long)
    if None in (ps, pl, cs, cl):
        return None
    if ps <= pl and cs > cl:
        return "golden"
    if ps >= pl and cs < cl:
        return "dead"
    return None


def decide(closes: Sequence[float], avg_price: Optional[float], cfg: StrategyConfig) -> Signal:
    """avg_price 가 None 이면 미보유, 값이 있으면 보유 중(평균단가)."""
    if not closes:
        return Signal("HOLD", "데이터 없음")
    price = closes[-1]
    c = cross(closes, cfg.short_window, cfg.long_window)

    if avg_price is not None and avg_price > 0:
        pnl = (price - avg_price) / avg_price * 100.0
        if pnl >= cfg.take_profit_pct:
            return Signal("SELL", f"익절 {pnl:+.2f}%")
        if pnl <= -cfg.stop_loss_pct:
            return Signal("SELL", f"손절 {pnl:+.2f}%")
        if cfg.sell_on_dead_cross and c == "dead":
            return Signal("SELL", f"데드크로스 ({pnl:+.2f}%)")
        return Signal("HOLD", f"보유 {pnl:+.2f}%")

    if c == "golden":
        return Signal("BUY", f"골든크로스 MA{cfg.short_window}>MA{cfg.long_window}")
    return Signal("HOLD", "신호 없음")
