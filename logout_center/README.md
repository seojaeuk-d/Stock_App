# 일괄 로그아웃 센터

다른 기기에 로그인된 내 계정(구글·네이버·카카오 등 18개 서비스)을 서비스별 공식 "로그인 기기 관리" 화면으로 바로 연결해
순서대로 로그아웃하고 진행 상황을 체크하는 정적 웹앱입니다. 서버는 HTML만 제공하며 사용자 데이터는 브라우저(localStorage)에만 저장됩니다.

## 로컬 실행
```bash
cd logout_center
docker build -t logout-center . && docker run --rm -p 8080:8080 logout-center
# http://localhost:8080
```

## Fly.io 배포
- 자동: GitHub 저장소 Settings → Secrets → Actions 에 `FLY_API_TOKEN` 추가 후 `logout_center/` 변경을 push 하거나
  Actions 탭에서 "Deploy Logout Center (Fly.io)" 워크플로를 수동 실행.
- 수동: `cd logout_center && fly deploy`
- 앱 이름 `logout-center`가 이미 사용 중이면 `fly.toml`과 워크플로의 앱 이름을 바꾸세요.
