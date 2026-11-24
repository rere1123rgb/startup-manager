# 시작 관리자 (StartupManager)

Windows에서 부팅·로그온 때 자동으로 켜지는 프로그램을 한 화면에서 보고 관리하는 트레이 앱입니다.

- 작업 스케줄러, 레지스트리 Run(사용자·모든 사용자), 시작프로그램 폴더의 항목과 실행 중 여부, 마지막 실행 결과를 한 번에 보여 줍니다.
- 항목을 지우지 않고 켜고 끕니다(작업 관리자의 '시작 앱'과 같은 방식). 필요 없는 항목은 백업을 남기고 목록에서 삭제할 수 있습니다.
- '관리 실행기'에 넣은 프로그램은 로그온 때 정한 순서·대기 시간·창 상태(보통/최소화)대로 켭니다.
- 이미 실행 중인 프로그램은 다시 켜지 않습니다. 명령줄로 구분할 수 있어 같은 `pythonw.exe` 로 도는 스크립트도 따로 다룹니다.
- 꺼지면 다시 켜기, 특정 경로(예: 네트워크·마운트 드라이브)가 사라지면 다시 켜기를 항목별로 고를 수 있습니다.
- Steam 게임(AppId)은 Steam 로그인이 끝난 뒤 하나씩 켭니다.

화면은 한국어입니다. Windows 10/11 64비트용입니다.

## 다운로드

[Releases](../../releases) 에서 `StartupManager-*.zip` 을 받아 압축을 풀고 `StartupManager.exe` 를 실행합니다. 설치는 필요 없습니다.

1. 폴더를 지우지 않을 곳에 둡니다.
2. `StartupManager.exe` 실행 → 위쪽 **로그온 실행기 등록**.
3. 이제 로그온할 때 트레이에 뜨고, 관리 실행기 항목을 켭니다.

자세한 사용법은 zip 안의 `사용법.txt` 를 보세요.

코드 서명이 없어 처음 실행할 때 Windows SmartScreen 경고가 뜰 수 있습니다(추가 정보 → 실행).
인터넷 통신은 하지 않습니다(업데이트 확인·통계·오류 전송 없음).

설정과 기록은 `%APPDATA%\StartupManager` 에 저장됩니다. exe 옆에 `config.json` 이 있으면 그 폴더를 씁니다(포터블 모드).

## 소스로 실행 / 빌드

Python 3.10 이상.

```
pip install -r requirements.txt
python gui.py              # 창
python gui.py --tray       # 트레이로 시작
python launcher.py         # 로그온 실행기 (창 없음)
python build.py            # build_out\ 에 exe 폴더와 zip 생성 (PyInstaller)
```

## 파일

| 파일 | 내용 |
|---|---|
| `core.py` | 항목 읽기·켜기/끄기·삭제, 관리 항목 실행(중복 확인·지연·최소화·다시 켜기), 로그온 작업 등록 |
| `gui.py` | PyQt6 트레이 GUI |
| `launcher.py` | 소스 실행용 로그온 실행기 진입점 |
| `app.py` | exe 진입점 (`--tray`, `--launch`) |
| `build.py` | PyInstaller 빌드 |
