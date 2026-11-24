# 세탁 알리미

ESP32-C3가 보낸 CdS 조도값으로 세탁기 표시등을 감지하고, 등록된 스마트폰에 Web Push를 보내는 앱입니다. 백엔드는 FastAPI와 SQLite, 프론트엔드는 React와 TypeScript를 사용합니다.

## 로컬 실행

Python과 Node.js가 설치되어 있어야 합니다. 아래 명령은 이 README가 있는 `laundry-alert` 폴더에서 시작합니다.

```sh
cd Frontend
npm ci
npm run build
cd ../Backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade 'pip>=26.2'
python -m pip install -r requirements.txt
python admin.py add A7F29C
python -m uvicorn main:app
```

[앱](http://localhost:8000/)에 접속하고 CLI가 발급한 센서 ID와 등록 코드로 연결합니다. API 계약은 [Swagger UI](http://localhost:8000/docs)에서 확인할 수 있습니다. 개발 서버를 사용할 때는 `Frontend`에서 `npm run dev`를 실행하고, FastAPI를 `LAUNDRY_ORIGIN=http://localhost:5173`으로 시작합니다. 쿠키의 Secure 속성은 HTTP localhost에서만 해제됩니다.

상태, 기록 및 알림을 공유할 기기에는 등록 코드를 사용합니다. 감지를 취소하거나 재시작할 기기는 같은 입력란에 **관리자 등록 코드**를 입력합니다. 공유 코드는 30일간 유효하고, 관리자 등록 코드는 7일 안에 한 번 사용할 수 있습니다. 기존 DB에서는 센서별 최초 유효 등록 기기에 제어 권한을 부여합니다. 기기 인증의 최대 유효기간은 90일입니다.

### 센서 연결

센서는 5초마다 `POST /api/v1/sensor/readings`에 다음 형식으로 데이터를 보냅니다. 토큰은 센서에만 저장합니다. 부팅마다 고유한 `boot_id`를 생성하고, 같은 부팅에서 `sequence`를 증가시킵니다. 연결 복구 시 오래된 샘플을 다시 보내지 않고 최신값부터 전송합니다.

```json
{"sensor_id":"A7F29C","boot_id":"example-boot-id","sequence":1,"brightness":350}
```

요청에 `Authorization: Bearer <센서 토큰>`과 `Content-Type: application/json`을 붙입니다. 기기 화면의 상태 변경 요청에는 동일 출처와 `X-Laundry-Request: 1` 헤더가 필요합니다.

감지는 각 밝기 수준에서 비슷한 값이 연속 두 번 들어온 뒤 시작합니다. 기본 최소 조도 차이는 원시값 80이며, 서버의 `LAUNDRY_MIN_DELTA`에서 조정합니다. 기존 DB를 처음 열 때는 이전 버전의 밝기 기준을 초기화하고 새로 학습합니다. 표시등 ON 10초에 작동 중으로 바뀌고, 작동 중 OFF 30초에 완료됩니다. 측정 공백이나 부팅 변경이 발생하면 연속 판정은 새로 시작합니다. 작동 중 데이터가 30초 동안 끊기면 오류 상태와 연결 오류 알림이 발생하고, 데이터가 돌아오면 같은 작업에서 판정을 재개합니다.

## 운영 설정

- `Backend/.env.example`을 기준으로 운영 환경 변수를 설정합니다. Web Push에 VAPID 공개 키, 개인 키 또는 개인 키 PEM 파일 경로, 운영자 연락처가 필요합니다. `Backend/.venv/bin/vapid --gen`으로 키 쌍을 생성하고 `Backend/.venv/bin/vapid --applicationServerKey`로 브라우저용 공개 키를 확인할 수 있습니다.
- Raspberry Pi에서 `deploy/`의 systemd 서비스와 백업 타이머를 사용합니다. 예시 경로는 `/opt/laundry-alert`, 상태 디렉터리는 `/var/lib/laundry-alert`, 서비스 계정은 `laundry`입니다. 상태 디렉터리는 서비스 계정에 쓰기 권한을 주고, 환경 파일의 접근 권한을 제한합니다. Uvicorn 프로세스는 한 개로 실행합니다.
- Cloudflare Tunnel은 `deploy/cloudflared.example.yml`의 터널 ID와 호스트명을 교체해 적용합니다. FastAPI가 빌드된 프론트엔드와 API를 같은 HTTPS 도메인에서 제공합니다. `LAUNDRY_ORIGIN`을 해당 도메인으로 설정합니다.
- 아이폰은 Safari 공유 메뉴에서 앱을 홈 화면에 추가합니다. 홈 화면에서 앱을 연 뒤 **설정 → 푸시 알림**을 켭니다.
- 등록을 다시 하거나 서버의 VAPID 키를 교체한 뒤에는 각 기기에서 **설정 → 푸시 알림**을 켜 구독을 등록합니다.
- `python admin.py rotate-code A7F29C`는 기존 스마트폰 인증을 폐기하고 새 등록 코드를 생성합니다. `python admin.py rotate-token A7F29C`는 센서 토큰을 즉시 교체합니다.
- `python admin.py share-code A7F29C`는 기존 기기 인증을 유지하면서 공유 코드만 새로 발급합니다. `python admin.py owner-code A7F29C`는 새 관리자 등록 코드를 발급합니다.
- `python admin.py list-devices A7F29C`로 기기 식별자를 확인하고 `python admin.py revoke-device A7F29C <installation_id>`로 해당 기기만 해제할 수 있습니다.
- 자동화에서는 발급 명령에 `--output /var/lib/laundry-alert/A7F29C.secrets`를 지정합니다. 새 파일에 소유자 읽기·쓰기 권한을 적용합니다.
- `python admin.py backup /var/lib/laundry-alert/backups`는 일별 DB 백업을 만들고 최근 7개를 보관합니다.

## 검증

`Backend`에서 `python -m unittest -v`를 실행합니다. 실제 10초·30초를 기다리는 대신 가상 시계와 임시 SQLite로 검증합니다. 센서 Wi-Fi 설정과 재전송 동작은 펌웨어에서, 실제 푸시 수신과 서비스 자동 실행은 스마트폰 및 Raspberry Pi에서 확인해야 합니다.
