"""키움증권 REST API 클라이언트 (모의투자/실전 공용).

사용하는 API
- au10001  POST /oauth2/token          접근토큰 발급
- au10002  POST /oauth2/revoke         접근토큰 폐기
- ka10001  POST /api/dostk/stkinfo     주식기본정보 (현재가)
- ka10081  POST /api/dostk/chart       주식일봉차트
- kt10000  POST /api/dostk/ordr        주식 매수주문
- kt10001  POST /api/dostk/ordr        주식 매도주문
- kt00001  POST /api/dostk/acnt        예수금상세현황 (주문가능금액)
- kt00018  POST /api/dostk/acnt        계좌평가잔고내역 (보유종목)

필드명은 키움 REST API 문서 기준입니다. 응답 숫자는 "+00070000" 처럼
부호/0 패딩이 붙은 문자열로 오기 때문에 to_int/to_price 로 변환합니다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

REAL_HOST = "https://api.kiwoom.com"
MOCK_HOST = "https://mockapi.kiwoom.com"  # 모의투자는 KRX 종목만 지원


class KiwoomAPIError(RuntimeError):
    def __init__(self, message: str, return_code: Any = None, api_id: str = ""):
        super().__init__(message)
        self.return_code = return_code
        self.api_id = api_id


def to_int(value: Any, default: int = 0) -> int:
    """'+0007000' / '-1,500' / '' 같은 키움 문자열을 int 로."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(",", "")
    if s in ("", "+", "-"):
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def to_price(value: Any) -> int:
    """가격 필드는 부호가 '전일 대비 방향'이므로 절대값을 씁니다."""
    return abs(to_int(value))


def normalize_code(code: str) -> str:
    """'A005930' -> '005930'."""
    code = (code or "").strip()
    if code[:1].upper() == "A" and code[1:].isdigit():
        return code[1:]
    return code


@dataclass
class Holding:
    code: str
    name: str
    qty: int
    avg_price: int
    cur_price: int
    sellable_qty: int

    @property
    def unrealized_pnl(self) -> int:
        return (self.cur_price - self.avg_price) * self.qty

    @property
    def pnl_pct(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.cur_price - self.avg_price) / self.avg_price * 100.0


class KiwoomClient:
    def __init__(
        self,
        appkey: str,
        secretkey: str,
        mock: bool = True,
        min_interval: float = 0.25,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ):
        if not appkey or not secretkey:
            raise ValueError("KIWOOM_APPKEY / KIWOOM_SECRETKEY 가 비어 있습니다 (.env 확인).")
        self.appkey = appkey
        self.secretkey = secretkey
        self.mock = mock
        self.host = MOCK_HOST if mock else REAL_HOST
        self.min_interval = min_interval
        self.timeout = timeout
        self.session = session or requests.Session()
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 토큰
    def _issue_token(self) -> None:
        url = f"{self.host}/oauth2/token"
        body = {"grant_type": "client_credentials", "appkey": self.appkey, "secretkey": self.secretkey}
        resp = self.session.post(
            url, json=body, headers={"Content-Type": "application/json;charset=UTF-8", "api-id": "au10001"},
            timeout=self.timeout,
        )
        data = self._parse_json(resp, "au10001")
        token = data.get("token")
        if not token:
            raise KiwoomAPIError(f"토큰 발급 실패: {data.get('return_msg')}", data.get("return_code"), "au10001")
        self._token = token
        try:
            self._token_expires = datetime.strptime(str(data.get("expires_dt")), "%Y%m%d%H%M%S")
        except ValueError:
            self._token_expires = datetime.now() + timedelta(hours=12)
        log.info("접근토큰 발급 완료 (%s, 만료 %s)", "모의" if self.mock else "실전", self._token_expires)

    def _ensure_token(self) -> str:
        if not self._token or not self._token_expires or datetime.now() >= self._token_expires - timedelta(minutes=10):
            self._issue_token()
        assert self._token
        return self._token

    def revoke(self) -> None:
        if not self._token:
            return
        try:
            self.session.post(
                f"{self.host}/oauth2/revoke",
                json={"appkey": self.appkey, "secretkey": self.secretkey, "token": self._token},
                headers={"Content-Type": "application/json;charset=UTF-8", "api-id": "au10002"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:  # 폐기 실패는 치명적이지 않음
            log.warning("토큰 폐기 실패: %s", e)
        self._token = None

    # ------------------------------------------------------------------ 공통 호출
    @staticmethod
    def _parse_json(resp: requests.Response, api_id: str) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise KiwoomAPIError(f"[{api_id}] HTTP {resp.status_code} 비정상 응답: {resp.text[:200]}", api_id=api_id)
        if resp.status_code >= 400:
            raise KiwoomAPIError(
                f"[{api_id}] HTTP {resp.status_code}: {data.get('return_msg', data)}", data.get("return_code"), api_id
            )
        rc = data.get("return_code", 0)
        if to_int(rc) != 0:
            raise KiwoomAPIError(f"[{api_id}] {data.get('return_msg')}", rc, api_id)
        return data

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def call(self, path: str, api_id: str, body: dict, cont_yn: str = "", next_key: str = "") -> tuple[dict, dict]:
        """API 호출. (응답 JSON, 응답 헤더) 반환. 429/5xx/네트워크 오류는 재시도."""
        last_err: Optional[Exception] = None
        for attempt in range(4):
            self._throttle()
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {self._ensure_token()}",
                "api-id": api_id,
            }
            if cont_yn:
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key
            try:
                resp = self.session.post(f"{self.host}{path}", json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                log.warning("[%s] 네트워크 오류 (%d회): %s", api_id, attempt + 1, e)
                time.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code == 401 and attempt == 0:
                log.info("[%s] 토큰 만료로 재발급", api_id)
                self._token = None
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = KiwoomAPIError(f"[{api_id}] HTTP {resp.status_code}", api_id=api_id)
                time.sleep(1.0 * (attempt + 1))
                continue
            data = self._parse_json(resp, api_id)
            return data, {"cont-yn": resp.headers.get("cont-yn", "N"), "next-key": resp.headers.get("next-key", "")}
        raise KiwoomAPIError(f"[{api_id}] 재시도 초과: {last_err}", api_id=api_id)

    # ------------------------------------------------------------------ 시세
    def get_current_price(self, code: str) -> int:
        data, _ = self.call("/api/dostk/stkinfo", "ka10001", {"stk_cd": code})
        price = to_price(data.get("cur_prc"))
        if price <= 0:
            raise KiwoomAPIError(f"[ka10001] {code} 현재가 없음", api_id="ka10001")
        return price

    def get_daily_closes(self, code: str, base_date: Optional[str] = None) -> list[tuple[str, int]]:
        """일봉 종가 [(YYYYMMDD, 종가)] 오래된 순. 수정주가 적용."""
        base_date = base_date or datetime.now().strftime("%Y%m%d")
        data, _ = self.call("/api/dostk/chart", "ka10081", {"stk_cd": code, "base_dt": base_date, "upd_stkpc_tp": "1"})
        rows = data.get("stk_dt_pole_chart_qry") or []
        out = [(str(r.get("dt")), to_price(r.get("cur_prc"))) for r in rows if r.get("dt") and to_price(r.get("cur_prc")) > 0]
        out.sort(key=lambda x: x[0])
        return out

    # ------------------------------------------------------------------ 주문
    def _order(self, api_id: str, code: str, qty: int, price: int = 0) -> str:
        if qty <= 0:
            raise ValueError("주문 수량은 1 이상이어야 합니다.")
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": code,
            "ord_qty": str(qty),
            "ord_uv": str(price) if price > 0 else "",
            "trde_tp": "0" if price > 0 else "3",  # 0: 보통(지정가), 3: 시장가
            "cond_uv": "",
        }
        data, _ = self.call("/api/dostk/ordr", api_id, body)
        return str(data.get("ord_no", ""))

    def buy(self, code: str, qty: int, price: int = 0) -> str:
        """price=0 이면 시장가."""
        return self._order("kt10000", code, qty, price)

    def sell(self, code: str, qty: int, price: int = 0) -> str:
        return self._order("kt10001", code, qty, price)

    # ------------------------------------------------------------------ 계좌
    def get_orderable_cash(self) -> int:
        data, _ = self.call("/api/dostk/acnt", "kt00001", {"qry_tp": "3"})
        for key in ("ord_alow_amt", "100stk_ord_alow_amt", "d2_entra", "entr"):
            if key in data and to_int(data.get(key)) > 0:
                return to_int(data.get(key))
        return 0

    def get_holdings(self) -> dict[str, Holding]:
        holdings: dict[str, Holding] = {}
        cont_yn, next_key = "", ""
        for _ in range(20):  # 연속조회
            data, hdr = self.call(
                "/api/dostk/acnt", "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"}, cont_yn, next_key
            )
            for r in data.get("acnt_evlt_remn_indv_tot") or []:
                code = normalize_code(str(r.get("stk_cd", "")))
                qty = to_int(r.get("rmnd_qty"))
                if not code or qty <= 0:
                    continue
                holdings[code] = Holding(
                    code=code,
                    name=str(r.get("stk_nm", "")).strip(),
                    qty=qty,
                    avg_price=to_price(r.get("pur_pric")),
                    cur_price=to_price(r.get("cur_prc")),
                    sellable_qty=to_int(r.get("trde_able_qty"), qty),
                )
            if hdr.get("cont-yn") != "Y" or not hdr.get("next-key"):
                break
            cont_yn, next_key = "Y", hdr["next-key"]
        return holdings
