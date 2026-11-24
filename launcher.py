"""로그온 때 작업 스케줄러가 pythonw 로 켜는 실행기. 창 없이 관리 항목을 켜고 launcher.log 에 남긴다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

if __name__ == '__main__':
    try:
        core.run_all()
    except Exception as e:
        core.log(f'실행기 오류: {e!r}')
        raise
