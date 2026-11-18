"""자동매매 실행기.

    python -m autotrader.trader --check          # 연결 확인 (예수금·잔고·현재가 출력, 주문 없음)
    python -m autotrader.trader --dry-run        # 신호만 기록, 주문은 보내지 않음
    python -m autotrader.trader                  # 모의투자 자동매매 시작
    python -m autotrader.trader --once           # 한 번만 점검하고 종료

비상 정지: autotrader/STOP 파일을 만들면 다음 루프에서 즉시 종료합니다.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .kiwoom_client import Holding, KiwoomAPIError, KiwoomClient
from .risk import RiskConfig, RiskManager
from .strategy import StrategyConfig, decide

KST = ZoneInfo("Asia/Seoul")
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("autotrader")


# ---------------------------------------------------------------------- 설정
@dataclass
class AppConfig:
    mock: bool = True
    dry_run: bool = False
    watchlist: list[str] = field(default_factory=lambda: ["005930", "000660", "035420"])
    interval_sec: int = 60
    market_open: str = "09:05"
    market_close: str = "15:15"
    data_dir: str = os.path.join(PKG_DIR, "data")
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    @classmethod
    def load(cls, path: Optional[str]) -> "AppConfig":
        raw: dict[str, Any] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        elif path:
            log.warning("설정 파일 %s 이 없어 기본값을 사용합니다.", path)
        strat = StrategyConfig(**raw.pop("strategy", {}))
        risk = RiskConfig(**raw.pop("risk", {}))
        cfg = cls(strategy=strat, risk=risk, **raw)
        if not os.path.isabs(cfg.data_dir):
            cfg.data_dir = os.path.join(PKG_DIR, cfg.data_dir)
        cfg.watchlist = [str(c).zfill(6) for c in cfg.watchlist]
        return cfg


def load_env(*paths: str) -> None:
    """간단한 .env 로더 (KEY=VALUE). 이미 설정된 환경변수는 덮어쓰지 않습니다."""
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


# ---------------------------------------------------------------------- 트레이더
class Trader:
    def __init__(self, client: Any, cfg: AppConfig, risk: Optional[RiskManager] = None):
        self.client = client
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.risk = risk or RiskManager(cfg.risk, os.path.join(cfg.data_dir, "daily_state.json"))
        self.trades_path = os.path.join(cfg.data_dir, "trades.csv")
        self._daily_cache: dict[str, tuple[str, list[int]]] = {}
        self.stop_file = os.path.join(PKG_DIR, "STOP")

    # ---- 시간
    def in_market_hours(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        o, c = _hm(self.cfg.market_open), _hm(self.cfg.market_close)
        return o <= (now.hour, now.minute) < c

    # ---- 데이터
    def past_closes(self, code: str, today: str) -> list[int]:
        """오늘을 제외한 과거 종가 (하루 한 번만 조회해 캐시)."""
        cached = self._daily_cache.get(code)
        if cached and cached[0] == today:
            return cached[1]
        rows = self.client.get_daily_closes(code, today)
        closes = [c for d, c in rows if d < today]
        self._daily_cache[code] = (today, closes)
        return closes

    # ---- 기록
    def _log_trade(self, now: datetime, code: str, side: str, qty: int, price: int, reason: str, ord_no: str) -> None:
        new = not os.path.exists(self.trades_path)
        with open(self.trades_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["시각", "모드", "종목", "구분", "수량", "기준가", "사유", "주문번호"])
            mode = "DRY" if self.cfg.dry_run else ("모의" if self.cfg.mock else "실전")
            w.writerow([now.strftime("%Y-%m-%d %H:%M:%S"), mode, code, side, qty, price, reason, ord_no])

    # ---- 한 번 점검
    def run_once(self, now: Optional[datetime] = None) -> list[tuple[str, str, int]]:
        """모든 감시 종목을 한 번 점검. 실행(또는 dry-run)한 주문 목록 [(code, side, qty)] 반환."""
        now = now or datetime.now(KST)
        today = now.strftime("%Y%m%d")
        self.risk.roll_day(now.date().isoformat())

        holdings: dict[str, Holding] = self.client.get_holdings()
        cash = self.client.get_orderable_cash()
        self.risk.unrealized_pnl = sum(h.unrealized_pnl for h in holdings.values())
        n_positions = len(holdings)
        log.info(
            "점검: 보유 %d종목, 주문가능 %s원, 당일손익 %s원 (실현 %s / 평가 %s)",
            n_positions, f"{cash:,}", f"{self.risk.daily_pnl:,}",
            f"{self.risk.state.realized_pnl:,}", f"{self.risk.unrealized_pnl:,}",
        )

        orders: list[tuple[str, str, int]] = []
        codes = list(dict.fromkeys(list(holdings.keys()) + self.cfg.watchlist))
        for code in codes:
            try:
                price = self.client.get_current_price(code)
                closes = self.past_closes(code, today) + [price]
                h = holdings.get(code)
                sig = decide(closes, h.avg_price if h else None, self.cfg.strategy)

                if sig.action == "SELL" and h:
                    if code in self.risk.state.sold_today:
                        continue  # 이미 매도 주문 냄 (잔고 반영 대기)
                    ok, why = self.risk.can_sell()
                    qty = h.sellable_qty or h.qty
                    if not ok or qty <= 0:
                        log.warning("[%s] 매도 신호(%s) 보류: %s", code, sig.reason, why)
                        continue
                    ord_no = "DRY" if self.cfg.dry_run else self.client.sell(code, qty)
                    self.risk.record_sell(code, (price - h.avg_price) * qty)
                    self._log_trade(now, code, "매도", qty, price, sig.reason, ord_no)
                    log.info("[%s] 매도 %d주 @%s (%s) 주문번호=%s", code, qty, f"{price:,}", sig.reason, ord_no)
                    orders.append((code, "SELL", qty))
                    n_positions -= 1

                elif sig.action == "BUY" and not h:
                    qty, why = self.risk.buy_qty(code, price, n_positions, cash)
                    if qty <= 0:
                        log.info("[%s] 매수 신호(%s) 보류: %s", code, sig.reason, why)
                        continue
                    ord_no = "DRY" if self.cfg.dry_run else self.client.buy(code, qty)
                    self.risk.record_buy(code)
                    self._log_trade(now, code, "매수", qty, price, sig.reason, ord_no)
                    log.info("[%s] 매수 %d주 @%s (%s) 주문번호=%s", code, qty, f"{price:,}", sig.reason, ord_no)
                    orders.append((code, "BUY", qty))
                    n_positions += 1
                    cash -= qty * price
                else:
                    log.debug("[%s] %s @%s", code, sig.reason, f"{price:,}")
            except KiwoomAPIError as e:
                log.error("[%s] API 오류: %s", code, e)
        return orders

    # ---- 메인 루프
    def run_forever(self) -> None:
        log.info("자동매매 시작 — %s%s, 감시종목 %s",
                 "모의투자" if self.cfg.mock else "실전투자", " (DRY-RUN)" if self.cfg.dry_run else "",
                 ", ".join(self.cfg.watchlist))
        while True:
            if os.path.exists(self.stop_file):
                log.warning("STOP 파일 감지 — 종료합니다.")
                return
            now = datetime.now(KST)
            if self.in_market_hours(now):
                try:
                    self.run_once(now)
                except KiwoomAPIError as e:
                    log.error("점검 실패: %s", e)
                time.sleep(self.cfg.interval_sec)
            else:
                log.info("장 운영 시간 외 (%s) — 대기 중", now.strftime("%a %H:%M"))
                time.sleep(min(300, self.cfg.interval_sec * 5))


# ---------------------------------------------------------------------- CLI
def setup_logging(data_dir: str, verbose: bool) -> None:
    os.makedirs(data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(os.path.join(data_dir, "autotrader.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_client(cfg: AppConfig) -> KiwoomClient:
    if not cfg.mock and os.environ.get("KIWOOM_REAL_CONFIRM") != "YES":
        raise SystemExit(
            "실전투자 모드는 차단되어 있습니다. 충분히 모의투자로 검증한 뒤에만 "
            ".env 에 KIWOOM_REAL_CONFIRM=YES 를 추가하세요."
        )
    return KiwoomClient(os.environ.get("KIWOOM_APPKEY", ""), os.environ.get("KIWOOM_SECRETKEY", ""), mock=cfg.mock)


def check(client: KiwoomClient, cfg: AppConfig) -> None:
    print(f"[연결] {'모의투자' if cfg.mock else '실전'} 서버: {client.host}")
    print(f"[예수금] 주문가능금액: {client.get_orderable_cash():,}원")
    holdings = client.get_holdings()
    print(f"[잔고] {len(holdings)}종목")
    for h in holdings.values():
        print(f"   {h.code} {h.name} {h.qty}주 평균 {h.avg_price:,} 현재 {h.cur_price:,} ({h.pnl_pct:+.2f}%)")
    for code in cfg.watchlist:
        print(f"[시세] {code}: {client.get_current_price(code):,}원")


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="키움 REST API 자동매매 (모의투자)")
    ap.add_argument("--config", default=os.path.join(PKG_DIR, "config.json"))
    ap.add_argument("--check", action="store_true", help="연결/잔고 확인만")
    ap.add_argument("--once", action="store_true", help="한 번만 점검")
    ap.add_argument("--dry-run", action="store_true", help="주문을 보내지 않고 신호만 기록")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    load_env(os.path.join(PKG_DIR, ".env"), os.path.join(os.path.dirname(PKG_DIR), ".env"))
    cfg = AppConfig.load(args.config)
    if args.dry_run:
        cfg.dry_run = True
    setup_logging(cfg.data_dir, args.verbose)

    try:
        client = build_client(cfg)
    except ValueError as e:
        raise SystemExit(f"설정 오류: {e}")
    try:
        if args.check:
            check(client, cfg)
        elif args.once:
            Trader(client, cfg).run_once()
        else:
            Trader(client, cfg).run_forever()
    except KeyboardInterrupt:
        log.info("사용자 중단")
    finally:
        client.revoke()


if __name__ == "__main__":
    main()
