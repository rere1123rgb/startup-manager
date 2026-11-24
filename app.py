"""배포본(exe) 진입점.  StartupManager.exe            → 창
                     StartupManager.exe --tray     → 트레이로 시작
                     StartupManager.exe --launch   → 로그온 실행기 (창 없음)
"""
import sys

if __name__ == '__main__':
    if '--launch' in sys.argv:
        import core
        try:
            core.run_all()
        except Exception as e:
            core.log(f'실행기 오류: {e!r}')
            raise
    else:
        import gui
        sys.exit(gui.main())
