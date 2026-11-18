# autotrader — 키움 REST API 자동매매 (모의투자)

키움증권 REST API로 국내 주식을 정해진 규칙대로 자동으로 사고파는 프로그램입니다.
기본값은 **모의투자 서버**(`mockapi.kiwoom.com`)이고, 실전 서버는 이중 잠금으로 막혀 있습니다.

> ⚠️ 자동매매는 버그·네트워크 장애·급변동으로 손실이 날 수 있습니다. 예시 전략은 수익을 보장하지 않습니다.
> 반드시 모의투자에서 충분히 검증하세요.

## 1. 준비

1. [키움 REST API 포털](https://openapi.kiwoom.com)에서 REST API 사용 신청을 하고 **모의투자** App Key와 Secret Key를 발급받습니다.
   허용 IP 등록이 필요하면 이 프로그램을 실행할 PC의 공인 IP를 등록하세요.
2. 키움 모의투자 신청: 영웅문 또는 홈페이지에서 상시 모의투자에 가입합니다.
3. Python 3.10 이상을 준비하고 라이브러리를 설치합니다.
   ```bash
   pip install -r autotrader/requirements.txt
   ```
4. 키와 설정 파일을 만듭니다. 두 파일 모두 `.gitignore`에 등록되어 있어 깃에 올라가지 않습니다.
   ```bash
   cp autotrader/.env.example autotrader/.env               # 앱키/시크릿키 입력
   cp autotrader/config.example.json autotrader/config.json # 감시종목·예산 조정
   ```

## 2. 실행 (저장소 루트에서)

| 명령 | 하는 일 |
|---|---|
| `python -m autotrader.trader --check` | 연결 확인: 예수금, 잔고, 감시종목 현재가를 출력합니다. **주문 없음** |
| `python -m autotrader.trader --dry-run` | 실제 주문 없이 매수/매도 신호만 로그에 남깁니다 |
| `python -m autotrader.trader --once` | 한 번만 점검하고 종료합니다 |
| `python -m autotrader.trader` | 장중(09:05~15:15, 평일) 60초마다 점검하며 자동매매합니다 |
| `python -m autotrader.backtest --code 005930` | 같은 전략을 과거 일봉으로 백테스트합니다 |

- **비상 정지**: `autotrader/STOP` 파일을 만들면 다음 루프에서 종료합니다. Ctrl+C로도 멈출 수 있습니다.
- **기록**: `autotrader/data/autotrader.log`(로그), `trades.csv`(매매내역), `daily_state.json`(당일 손익과 주문 수)

## 3. 예시 전략 (`strategy.py`)

| 구분 | 조건 (기본값) |
|---|---|
| 매수 | 5일 이동평균이 20일 이동평균을 아래에서 위로 돌파 (**골든크로스**), 오늘 현재가 포함 |
| 익절 | 평균단가 대비 **+3%** 이상 |
| 손절 | 평균단가 대비 **−2%** 이하 |
| 추가 매도 | 5일선이 20일선을 아래로 돌파 (**데드크로스**, 끌 수 있음) |
| 주문 방식 | 시장가 |

## 4. 안전장치 (`risk.py`, `config.json` → `risk`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `budget_per_stock` | 1,000,000원 | 종목당 매수 금액 |
| `max_order_amount` | 2,000,000원 | 주문 1건 금액 상한 (설정 실수 방지) |
| `max_positions` | 3 | 동시에 보유할 최대 종목 수 |
| `max_daily_loss` | 300,000원 | 실현+평가 손실이 이 금액을 넘으면 그날 신규 매수 중단 (손절 매도는 계속) |
| `max_orders_per_day` | 20 | 하루 최대 주문 횟수 |
| (고정) | — | 같은 날 이미 사거나 판 종목은 다시 사지 않음 |

**실전 전환**: `config.json`에 `"mock": false`를 넣고, `.env`에 `KIWOOM_REAL_CONFIRM=YES`도 넣어야 합니다. 실전 App Key도 따로 필요합니다.
둘 중 하나라도 빠지면 실행이 거부됩니다.

## 5. 사용 API

`au10001`/`au10002` 토큰 발급과 폐기, `ka10001` 현재가, `ka10081` 일봉,
`kt10000`/`kt10001` 매수와 매도, `kt00001` 주문가능금액, `kt00018` 계좌평가잔고.
필드명은 키움 REST API 문서 기준입니다. 처음 실행할 때 `--check`로 값이 제대로 나오는지 꼭 확인하세요.

## 6. 알려진 한계

- 실현손익은 신호가 난 시점의 가격으로 추정합니다. 시장가 실제 체결가와 다를 수 있습니다.
- 공휴일은 따로 판단하지 않습니다. 장이 열리지 않은 날에는 주문이 거부되고 오류만 로그에 남습니다.
- 미체결 주문을 관리하지 않습니다. 시장가 주문이라 대부분 바로 체결됩니다.

## 테스트

```bash
python -m pytest autotrader/tests -q
```
