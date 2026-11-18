"""autotrader 단위 테스트 (네트워크 없이 가짜 클라이언트 사용)."""

from __future__ import annotations

from datetime import datetime

import pytest

from autotrader.backtest import run_backtest
from autotrader.kiwoom_client import Holding, KiwoomClient, normalize_code, to_int, to_price
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import StrategyConfig, cross, decide, sma
from autotrader.trader import KST, AppConfig, Trader

CFG = StrategyConfig(short_window=2, long_window=4, take_profit_pct=3, stop_loss_pct=2)


# ------------------------------------------------------------ 파싱
def test_number_parsing():
    assert to_int("+00070000") == 70000
    assert to_int("-1,500") == -1500
    assert to_int("") == 0 and to_int(None) == 0
    assert to_price("-70000") == 70000
    assert normalize_code("A005930") == "005930"
    assert normalize_code("005930") == "005930"


# ------------------------------------------------------------ 전략
def test_sma_and_cross():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1], 2) is None
    assert cross([10, 10, 10, 10, 20], 2, 4) == "golden"
    assert cross([10, 10, 10, 10, 5], 2, 4) == "dead"
    assert cross([10, 10, 10, 10, 10], 2, 4) is None


def test_decide_buy_and_exits():
    assert decide([10, 10, 10, 10, 20], None, CFG).action == "BUY"
    assert decide([10, 10, 10, 10, 10], None, CFG).action == "HOLD"
    assert "익절" in decide([100, 100, 100, 100, 103], 100, CFG).reason
    assert "손절" in decide([100, 100, 100, 100, 98], 100, CFG).reason
    assert decide([100, 100, 100, 100, 101], 100, CFG).action == "HOLD"


def test_strategy_config_validation():
    with pytest.raises(ValueError):
        StrategyConfig(short_window=20, long_window=5)


# ------------------------------------------------------------ 리스크
def test_risk_limits(tmp_path):
    rm = RiskManager(RiskConfig(budget_per_stock=100_000, max_positions=2, max_daily_loss=10_000,
                                max_orders_per_day=3, max_order_amount=50_000),
                     str(tmp_path / "s.json"), today="2026-11-18")
    qty, _ = rm.buy_qty("005930", 10_000, 0, 1_000_000)
    assert qty == 5  # max_order_amount 5만원 상한
    assert rm.buy_qty("005930", 10_000, 2, 1_000_000)[0] == 0  # 최대 종목 수
    assert rm.buy_qty("005930", 10_000, 0, 5_000)[0] == 0      # 예수금 부족
    rm.record_buy("005930")
    assert rm.buy_qty("005930", 10_000, 0, 1_000_000)[0] == 0  # 당일 재매수 금지
    rm.record_sell("000660", -11_000)
    assert rm.loss_limit_hit()
    assert rm.buy_qty("035420", 10_000, 0, 1_000_000)[0] == 0
    # 재시작 시 같은 날 상태 복원
    rm2 = RiskManager(rm.cfg, str(tmp_path / "s.json"), today="2026-11-18")
    assert rm2.state.realized_pnl == -11_000 and rm2.state.order_count == 2
    rm2.roll_day("2026-11-19")
    assert rm2.state.realized_pnl == 0


# ------------------------------------------------------------ 트레이더
class FakeClient:
    def __init__(self, prices, closes, holdings=None, cash=10_000_000):
        self.prices, self.closes, self.holdings, self.cash = prices, closes, holdings or {}, cash
        self.orders = []

    def get_holdings(self):
        return dict(self.holdings)

    def get_orderable_cash(self):
        return self.cash

    def get_current_price(self, code):
        return self.prices[code]

    def get_daily_closes(self, code, base_date=None):
        return [(f"202611{10 + i:02d}", c) for i, c in enumerate(self.closes[code])]

    def buy(self, code, qty, price=0):
        self.orders.append(("BUY", code, qty))
        return "0001"

    def sell(self, code, qty, price=0):
        self.orders.append(("SELL", code, qty))
        return "0002"


def make_trader(tmp_path, client, dry_run=False):
    cfg = AppConfig(watchlist=["005930", "000660"], data_dir=str(tmp_path), strategy=CFG, dry_run=dry_run,
                    risk=RiskConfig(budget_per_stock=100_000, max_order_amount=100_000))
    return Trader(client, cfg)


NOW = datetime(2026, 11, 18, 10, 0, tzinfo=KST)


def test_trader_buys_on_golden_cross_and_sells_on_stop_loss(tmp_path):
    client = FakeClient(
        prices={"005930": 20_000, "000660": 98_000},
        closes={"005930": [10_000] * 4, "000660": [100_000] * 4},
        holdings={"000660": Holding("000660", "SK하이닉스", 3, 100_000, 98_000, 3)},
    )
    t = make_trader(tmp_path, client)
    orders = t.run_once(NOW)
    assert ("000660", "SELL", 3) in orders
    assert ("005930", "BUY", 5) in orders
    assert client.orders == [("SELL", "000660", 3), ("BUY", "005930", 5)]
    assert t.risk.state.realized_pnl == -6_000
    assert (tmp_path / "trades.csv").exists()
    # 같은 날 다시 돌려도 중복 주문 없음
    assert t.run_once(NOW) == []


def test_dry_run_sends_no_orders(tmp_path):
    client = FakeClient(prices={"005930": 20_000, "000660": 100_000},
                        closes={"005930": [10_000] * 4, "000660": [100_000] * 4})
    t = make_trader(tmp_path, client, dry_run=True)
    assert t.run_once(NOW) == [("005930", "BUY", 5)]
    assert client.orders == []


def test_market_hours(tmp_path):
    t = make_trader(tmp_path, FakeClient({}, {}))
    assert t.in_market_hours(NOW)
    assert not t.in_market_hours(NOW.replace(hour=8))
    assert not t.in_market_hours(datetime(2026, 11, 21, 10, 0, tzinfo=KST))  # 토요일


def test_backtest_runs():
    rows = [(f"d{i:03d}", p) for i, p in enumerate([100] * 5 + [110, 115, 120, 90, 90, 90, 100, 110])]
    trades = run_backtest(rows, CFG, budget=10_000)
    assert trades and all(t.qty > 0 for t in trades)


# ------------------------------------------------------------ HTTP 레이어
class FakeResp:
    def __init__(self, status, data, headers=None):
        self.status_code, self._data, self.headers, self.text = status, data, headers or {}, str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self.responses.pop(0)


def test_client_token_order_and_holdings_pagination():
    token = FakeResp(200, {"token": "T", "expires_dt": "20991231235959", "return_code": 0})
    s = FakeSession([
        token,
        FakeResp(200, {"ord_no": "00024", "return_code": 0}),
        FakeResp(200, {"acnt_evlt_remn_indv_tot": [
            {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "000000000003",
             "pur_pric": "000000070000", "cur_prc": "+000000071000", "trde_able_qty": "000000000003"}],
            "return_code": 0}, {"cont-yn": "Y", "next-key": "K1"}),
        FakeResp(200, {"acnt_evlt_remn_indv_tot": [], "return_code": 0}, {"cont-yn": "N"}),
    ])
    c = KiwoomClient("k", "s", mock=True, min_interval=0, session=s)
    assert c.buy("005930", 1) == "00024"
    url, body, hdr = s.calls[1]
    assert url == "https://mockapi.kiwoom.com/api/dostk/ordr"
    assert hdr["api-id"] == "kt10000" and hdr["authorization"] == "Bearer T"
    assert body["trde_tp"] == "3" and body["ord_qty"] == "1"
    h = c.get_holdings()
    assert h["005930"].qty == 3 and h["005930"].cur_price == 71_000
    assert s.calls[3][2]["next-key"] == "K1"


def test_client_raises_on_error_code():
    from autotrader.kiwoom_client import KiwoomAPIError
    s = FakeSession([FakeResp(200, {"token": "T", "expires_dt": "20991231235959", "return_code": 0}),
                     FakeResp(200, {"return_code": 20, "return_msg": "주문가능금액 부족"})])
    c = KiwoomClient("k", "s", min_interval=0, session=s)
    with pytest.raises(KiwoomAPIError, match="주문가능금액"):
        c.buy("005930", 1)
