"""리스크 관리: 종목당 예산, 최대 보유 종목 수, 일일 손실 한도, 일일 주문 횟수 제한.

하루 상태(실현손익·주문 수·당일 매수/매도 종목)는 JSON 파일에 저장되어
프로그램을 재시작해도 같은 날이면 이어집니다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class RiskConfig:
    budget_per_stock: int = 1_000_000   # 종목당 매수 금액(원)
    max_positions: int = 3              # 동시에 보유할 최대 종목 수
    max_daily_loss: int = 300_000       # 실현+평가손실이 이 금액을 넘으면 신규 매수 중단(원)
    max_orders_per_day: int = 20        # 하루 최대 주문 횟수
    max_order_amount: int = 2_000_000   # 주문 1건 최대 금액(원) — 오작동 방지용 상한


@dataclass
class DailyState:
    day: str = field(default_factory=lambda: date.today().isoformat())
    realized_pnl: int = 0
    order_count: int = 0
    bought_today: list[str] = field(default_factory=list)
    sold_today: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: RiskConfig, state_path: Optional[str] = None, today: Optional[str] = None):
        self.cfg = cfg
        self.state_path = state_path
        self.state = self._load(today or date.today().isoformat())
        self.unrealized_pnl = 0

    # -------------------------------------------------------------- 상태 저장
    def _load(self, today: str) -> DailyState:
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    raw = json.load(f)
                if raw.get("day") == today:
                    return DailyState(**raw)
            except (OSError, ValueError, TypeError):
                pass
        return DailyState(day=today)

    def _save(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state.__dict__, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def roll_day(self, today: str) -> None:
        if self.state.day != today:
            self.state = DailyState(day=today)
            self._save()

    # -------------------------------------------------------------- 판단
    @property
    def daily_pnl(self) -> int:
        return self.state.realized_pnl + self.unrealized_pnl

    def loss_limit_hit(self) -> bool:
        return self.daily_pnl <= -abs(self.cfg.max_daily_loss)

    def buy_qty(self, code: str, price: int, n_positions: int, cash: int) -> tuple[int, str]:
        """매수 가능 수량과 사유. 0 이면 매수 불가."""
        if price <= 0:
            return 0, "가격 오류"
        if self.state.order_count >= self.cfg.max_orders_per_day:
            return 0, "일일 주문 한도 도달"
        if self.loss_limit_hit():
            return 0, f"일일 손실 한도 도달 ({self.daily_pnl:,}원)"
        if n_positions >= self.cfg.max_positions:
            return 0, f"최대 보유 종목 수({self.cfg.max_positions}) 도달"
        if code in self.state.bought_today or code in self.state.sold_today:
            return 0, "오늘 이미 매매한 종목"
        budget = min(self.cfg.budget_per_stock, self.cfg.max_order_amount, cash)
        qty = budget // price
        if qty <= 0:
            return 0, f"예산/예수금 부족 (가용 {budget:,}원, 가격 {price:,}원)"
        return int(qty), "OK"

    def can_sell(self) -> tuple[bool, str]:
        # 손절/익절 매도는 손실 한도와 무관하게 허용, 주문 횟수만 확인
        if self.state.order_count >= self.cfg.max_orders_per_day:
            return False, "일일 주문 한도 도달"
        return True, "OK"

    # -------------------------------------------------------------- 기록
    def record_buy(self, code: str) -> None:
        self.state.order_count += 1
        if code not in self.state.bought_today:
            self.state.bought_today.append(code)
        self._save()

    def record_sell(self, code: str, pnl: int) -> None:
        self.state.order_count += 1
        self.state.realized_pnl += int(pnl)
        if code not in self.state.sold_today:
            self.state.sold_today.append(code)
        self._save()
