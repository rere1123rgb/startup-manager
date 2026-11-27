"""시작 관리자 - 핵심 로직 (GUI 와 로그온 실행기가 같이 쓴다)

출처별 자동 실행 항목 읽기, 켜기/끄기, '관리 실행기'로 옮기기/되돌리기,
관리 항목 실행(중복 실행 방지, 지연, 최소화), 로그온 작업 등록을 다룬다.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import os
import struct
import subprocess
import sys
import threading
import time
import uuid
import winreg
from dataclasses import dataclass, field
from pathlib import Path

import pythoncom
import pywintypes
import win32api
import win32con
import win32gui
import win32process
import win32com.client
from win32com.shell import shell, shellcon

# 배포본(PyInstaller exe)이면 설정·기록을 %APPDATA%\StartupManager 에, 소스 실행이면 이 폴더에 둔다.
FROZEN = bool(getattr(sys, 'frozen', False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('APPDATA', str(APP_DIR))) / 'StartupManager' if FROZEN else APP_DIR
if FROZEN and (APP_DIR / 'config.json').exists():
    DATA_DIR = APP_DIR  # 포터블 모드: exe 옆에 config.json 이 있으면 그 폴더를 쓴다
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / 'config.json'
LASTRUN_PATH = DATA_DIR / 'last_run.json'
LOG_PATH = DATA_DIR / 'launcher.log'
SELF_ITEM_NAME = '시작 관리자 (트레이)'
LAUNCHER_TASK = 'StartupManager'   # 로그온 때 launcher.py 를 켜는 작업 이름

RUN_SUBKEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN32_SUBKEY = r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'
APPROVED = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved'

SOURCE_LABELS = {
    'managed': '시작 관리자가 켜는 항목',
    'task': '작업 스케줄러',
    'run_hkcu': '레지스트리 Run (사용자)',
    'run_hklm': '레지스트리 Run (모든 사용자)',
    'run_hklm32': '레지스트리 Run (모든 사용자, 32비트)',
    'folder_user': '시작프로그램 폴더 (사용자)',
    'folder_common': '시작프로그램 폴더 (모든 사용자)',
}

# 출처별: (레지스트리 루트, Run 키 경로 또는 None, StartupApproved 하위 이름)
_SOURCE_REG = {
    'run_hkcu': (winreg.HKEY_CURRENT_USER, RUN_SUBKEY, 'Run'),
    'run_hklm': (winreg.HKEY_LOCAL_MACHINE, RUN_SUBKEY, 'Run'),
    'run_hklm32': (winreg.HKEY_LOCAL_MACHINE, RUN32_SUBKEY, 'Run32'),
    'folder_user': (winreg.HKEY_CURRENT_USER, None, 'StartupFolder'),
    'folder_common': (winreg.HKEY_LOCAL_MACHINE, None, 'StartupFolder'),
}


# ───────────────────────── 공통 도우미 ─────────────────────────

def now_text() -> str:
    return dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


_log_lock = threading.Lock()


def log(message: str) -> None:
    """콘솔(있으면)과 launcher.log 에 한 줄 남긴다."""
    line = f'[{now_text()}] {message}'
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


def read_log_tail(lines: int = 300) -> str:
    if not LOG_PATH.exists():
        return ''
    text = LOG_PATH.read_text(encoding='utf-8', errors='replace').splitlines()
    return '\n'.join(text[-lines:])


def split_command(command: str) -> tuple[str, str]:
    """'"C:\\a b\\x.exe" -opt' → ('C:\\a b\\x.exe', '-opt'). 따옴표 없는 공백 경로도 .exe 까지 자른다."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end], command[end + 1:].strip()
    low = command.lower()
    pos = low.find('.exe')
    if pos >= 0:
        return command[:pos + 4], command[pos + 4:].strip()
    parts = command.split(' ', 1)
    return parts[0], (parts[1] if len(parts) > 1 else '')


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_elevated(exe: str, params: str, timeout: int = 60) -> int:
    """관리자 권한으로 실행하고 끝날 때까지 기다린다 (UAC 확인 창이 뜬다). 종료 코드 반환."""
    info = shell.ShellExecuteEx(fMask=0x40 | 0x8000,
                                lpVerb='runas', lpFile=exe, lpParameters=params, nShow=win32con.SW_HIDE)
    handle = info['hProcess']
    ctypes.windll.kernel32.WaitForSingleObject(int(handle), timeout * 1000)
    code = win32process.GetExitCodeProcess(handle)
    win32api.CloseHandle(handle)
    return code


# ───────────────────────── 프로세스·창 ─────────────────────────

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                ('szExeFile', ctypes.c_wchar * 260)]


def process_table() -> dict[str, list[int]]:
    """실행 중인 프로세스: {exe 이름(소문자): [pid, ...]}"""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    table: dict[str, list[int]] = {}
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        table.setdefault(entry.szExeFile.lower(), []).append(entry.th32ProcessID)
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return table


def is_running(process_name: str, table: dict | None = None) -> bool:
    if not process_name:
        return False
    table = table if table is not None else process_table()
    name = process_name.lower()
    if not name.endswith('.exe'):
        name += '.exe'
    return name in table


PYTHON_EXES = ('python.exe', 'pythonw.exe', 'py.exe', 'pyw.exe')


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', wt.USHORT), ('MaximumLength', wt.USHORT), ('Buffer', ctypes.c_void_p)]


def process_cmdline(pid: int) -> str | None:
    """프로세스 명령줄. NtQueryInformationProcess(ProcessCommandLineInformation=60)는
    PROCESS_QUERY_LIMITED_INFORMATION 권한만 있으면 되므로 관리자 권한 프로세스도 읽힌다(WMI 는 못 읽음)."""
    k32, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
    k32.OpenProcess.restype = wt.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        size = wt.ULONG(0)
        ntdll.NtQueryInformationProcess(wt.HANDLE(h), 60, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQueryInformationProcess(wt.HANDLE(h), 60, buf, size, ctypes.byref(size))
        if status != 0:
            return None
        us = _UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ''
    finally:
        k32.CloseHandle(wt.HANDLE(h))


def process_cmdlines(exe_name: str, table: dict | None = None) -> dict[int, str | None]:
    """이름이 exe_name 인 프로세스들의 {pid: 명령줄}. 못 읽으면 None."""
    table = table if table is not None else process_table()
    return {pid: process_cmdline(pid) for pid in table.get(exe_name.lower(), [])}


def find_pids(item: 'Item', table: dict | None = None) -> list[int]:
    """관리 항목에 해당하는 실행 중 프로세스. 확인 글자(match)가 있으면 명령줄까지 본다."""
    name = item.process_name().lower()
    if not name:
        return []
    if not name.endswith('.exe'):
        name += '.exe'
    table = table if table is not None else process_table()
    pids = table.get(name, [])
    match = item.match_text().lower()
    if not pids or not match:
        return list(pids)
    found, unknown = [], []
    for pid, cmd in process_cmdlines(name, table).items():
        if cmd is None:
            unknown.append(pid)
        elif match in cmd.lower():
            found.append(pid)
    if not found and unknown:
        # 명령줄을 못 읽은 같은 이름 프로세스가 있으면 '실행 중일 수 있음'으로 보고 켜지 않는다
        # (잘못 판단해 계속 다시 켜면 관리자 권한 앱은 UAC 창이 반복해서 뜨므로, 안 켜는 쪽이 안전).
        return unknown
    return found


def item_running(item: 'Item', table: dict | None = None) -> bool | None:
    if not item.process_name():
        return None
    return bool(find_pids(item, table))


def path_ok(path: str, timeout: float = 10) -> bool:
    """경로가 있는지. 끊긴 네트워크/마운트 드라이브는 오래 멈출 수 있어 시간 제한을 둔다."""
    if not path:
        return True
    result = []
    t = threading.Thread(target=lambda: result.append(os.path.exists(path)), daemon=True)
    t.start()
    t.join(timeout)
    return bool(result and result[0])


def terminate_pids(pids: list[int]) -> list[int]:
    """끝내지 못한 pid 목록을 돌려준다."""
    failed = []
    for pid in pids:
        try:
            h = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
            win32api.TerminateProcess(h, 1)
            win32api.CloseHandle(h)
        except pywintypes.error:
            failed.append(pid)
    return failed


def visible_windows(pids: set[int]) -> list[int]:
    """해당 프로세스들의 보이는 최상위 창.

    창 제목을 비우거나 창을 숨기는 유틸리티도 있으므로, 제목으로는 판단하지 않고
    숨겨진 창(IsWindowVisible=False)은 건드리지 않는다(최소화하면 다시 보이게 되므로).
    """
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and not win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            if win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
                ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                if not ex & win32con.WS_EX_TOOLWINDOW:
                    found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found


MINIMIZE_RETRY_LIMIT = 5  # 최소화한 창이 이보다 많이 다시 펼쳐지면 프로그램이 스스로 되돌리는 것으로 보고 그만둠


def keep_minimized(item: 'Item', label: str, wait_seconds: int = 120, hold_seconds: int = 20) -> None:
    """창이 뜨기를 기다렸다 최소화하고, 그 뒤 hold_seconds 동안 다시 펼쳐지면 또 최소화한다.

    게임처럼 처음에 한두 번 다시 펼쳐지는 건 따라가서 최소화하지만, 프로그램이 스스로 계속 되돌리면
    (창 숨김 유틸 등) 서로 싸우게 되므로 MINIMIZE_RETRY_LIMIT 번 넘게 펼쳐지면 그만둔다.
    """
    deadline = time.time() + wait_seconds
    first_done = None
    count = 0
    gave_up = False
    while time.time() < deadline and not gave_up:
        pids = set(find_pids(item))
        for hwnd in visible_windows(pids):
            if not win32gui.IsIconic(hwnd):
                if count > MINIMIZE_RETRY_LIMIT:
                    gave_up = True
                    break
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                count += 1
                if first_done is None:
                    first_done = time.time()
                    deadline = first_done + hold_seconds
        time.sleep(0.5)
    if first_done is None:
        log(f'{label}: {wait_seconds}초 동안 창이 안 보여 최소화 못 함')
    elif gave_up:
        log(f'{label}: 최소화해도 프로그램이 스스로 계속 다시 펼쳐서 ({count - 1}번) 그만둠 '
            f'— 이 항목은 창 상태를 "보통"으로 두는 게 맞을 수 있음')
    else:
        log(f'{label}: 최소화함' + (f' (다시 펼쳐져 {count - 1}번 더)' if count > 1 else ''))


# ───────────────────────── 설정 (관리 항목) ─────────────────────────

@dataclass
class Item:
    """관리 실행기가 로그온 때 켜는 항목 하나."""
    name: str
    type: str = 'exe'             # exe | steam | task
    path: str = ''                # exe: 실행 파일
    args: str = ''                # exe: 인수
    workdir: str = ''             # exe: 작업 폴더 (비우면 실행 파일 폴더)
    appid: int = 0                # steam: 게임 AppId
    task: str = ''                # task: 작업 스케줄러 작업 이름
    process: str = ''             # 중복 실행 확인·최소화에 쓰는 프로세스 이름 (예: MyApp.exe)
    delay: int = 0                # 이 항목을 켜기 전 기다리는 초
    window: str = 'normal'        # normal | minimized
    enabled: bool = True
    origin: dict | None = None    # 옮겨 오기 전 출처 {'source': 'run_hkcu', 'name': 'MyApp'}
    match: str = ''               # 명령줄에 이 글자가 들어 있는 프로세스만 '같은 프로그램'으로 봄 (예: my_script.py)
    restart: bool = False         # 꺼지면 다시 켜기 (실행기가 주기적으로 확인)
    check_path: str = ''          # 이 경로가 있어야 정상으로 봄 (예: Z:\). 프로세스는 있는데 경로가 없으면 끄고 다시 켬
    check_every: int = 0          # 꺼졌는지 확인하는 주기(초). 0이면 기본값 WATCH_INTERVAL
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def _is_python(self) -> bool:
        return self.type == 'exe' and Path(self.path).name.lower() in PYTHON_EXES

    def process_name(self) -> str:
        if self.process == '-':  # 중복 확인 안 함
            return ''
        if self.process:
            return self.process
        if self.type == 'exe' and self.path:
            # python/pythonw 는 여러 스크립트가 같이 쓰므로, 스크립트 이름(match_text)을 알 때만 확인한다.
            if self._is_python() and not self.match_text():
                return ''
            return Path(self.path).name
        return ''

    def match_text(self) -> str:
        """명령줄 확인 글자. 직접 정한 게 없고 python 항목이면 스크립트 파일 이름을 쓴다."""
        if self.match:
            return self.match
        if self._is_python() and self.args:
            script, _ = split_command(self.args) if self.args.strip().startswith('"') else (self.args.split()[0], '')
            name = Path(script.strip('"')).name
            if name.lower().endswith(('.py', '.pyw')):
                return name
        return ''

    def watch_interval(self) -> int:
        return self.check_every if self.check_every and self.check_every > 0 else WATCH_INTERVAL

    def describe(self) -> str:
        if self.type == 'steam':
            return f'steam://rungameid/{self.appid}'
        if self.type == 'task':
            return f'작업 스케줄러 작업 "{self.task}" 실행'
        return f'"{self.path}" {self.args}'.strip()


def interval_text(sec: int) -> str:
    if sec % 3600 == 0:
        return f'{sec // 3600}시간'
    if sec % 60 == 0:
        return f'{sec // 60}분'
    if sec > 60:
        return f'{sec // 60}분 {sec % 60}초'
    return f'{sec}초'


def load_items() -> list[Item]:
    if not CONFIG_PATH.exists():
        return []
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    known = Item.__dataclass_fields__.keys()
    return [Item(**{k: v for k, v in raw.items() if k in known}) for raw in data.get('items', [])]


def save_items(items: list[Item]) -> None:
    data = {'items': [item.__dict__ for item in items]}
    tmp = CONFIG_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(CONFIG_PATH)


def load_last_run() -> dict:
    try:
        return json.loads(LASTRUN_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_last_run(data: dict) -> None:
    tmp = LASTRUN_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(LASTRUN_PATH)


# ───────────────────────── 항목 목록 (현황) ─────────────────────────

@dataclass
class Entry:
    key: str                 # 출처:이름 (고유)
    source: str              # SOURCE_LABELS 의 키
    name: str
    command: str
    enabled: bool
    running: bool | None     # 알 수 없으면 None
    detail: str = ''         # 마지막 실행 결과 등
    process: str = ''        # 실행 중 판단에 쓴 프로세스 이름
    managed_id: str = ''     # source == managed 일 때 Item.id
    adopted_by: str = ''     # 관리 실행기로 옮겨진 원래 항목이면 그 Item.id
    needs_admin: bool = False

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)


def _approved_values(root, sub: str) -> dict[str, bytes]:
    try:
        key = winreg.OpenKey(root, f'{APPROVED}\\{sub}')
    except OSError:
        return {}
    values = {}
    i = 0
    while True:
        try:
            name, data, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        if isinstance(data, bytes):
            values[name] = data
        i += 1
    winreg.CloseKey(key)
    return values


def _is_approved(data: bytes | None) -> bool:
    # 첫 바이트가 짝수(02, 06 등)면 켜짐, 홀수(01, 03)면 꺼짐. 값이 없으면 켜짐.
    return data is None or len(data) == 0 or data[0] % 2 == 0


def _run_values(root, subkey: str) -> dict[str, str]:
    try:
        key = winreg.OpenKey(root, subkey)
    except OSError:
        return {}
    values = {}
    i = 0
    while True:
        try:
            name, data, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        if isinstance(data, str) and name:
            values[name] = data
        i += 1
    winreg.CloseKey(key)
    return values


def _startup_folders() -> dict[str, Path]:
    return {
        'folder_user': Path(shell.SHGetFolderPath(0, shellcon.CSIDL_STARTUP, None, 0)),
        'folder_common': Path(shell.SHGetFolderPath(0, shellcon.CSIDL_COMMON_STARTUP, None, 0)),
    }


def resolve_shortcut(path: Path) -> tuple[str, str, str]:
    """바로 가기 → (대상, 인수, 작업 폴더)"""
    try:
        sc = win32com.client.Dispatch('WScript.Shell').CreateShortcut(str(path))
        return sc.TargetPath, sc.Arguments, sc.WorkingDirectory
    except Exception:
        return str(path), '', ''


_TASK_RESULT = {0: '성공', 0x41301: '실행 중', 0x41303: '아직 실행 안 됨', 0x41306: '사용자가 종료',
                0x8004131F: '이미 실행 중이라 건너뜀', 1: '오류 (코드 1)', 2: '오류 (코드 2)'}


def task_result_text(code: int) -> str:
    code &= 0xFFFFFFFF
    return _TASK_RESULT.get(code, f'코드 0x{code:X}')


def _task_service():
    pythoncom.CoInitialize()
    svc = win32com.client.Dispatch('Schedule.Service')
    svc.Connect()
    return svc


def _task_entries(table) -> list[Entry]:
    entries = []
    svc = _task_service()
    for task in svc.GetFolder('\\').GetTasks(1):  # 1 = 숨김 작업 포함
        name = task.Name
        definition = task.Definition
        actions = []
        for action in definition.Actions:
            if action.Type == 0:  # 실행 동작
                actions.append(f'{action.Path} {action.Arguments or ""}'.strip())
        trig_names = {1: '시간', 2: '매일', 3: '매주', 4: '매월', 5: '매월(요일)', 6: '유휴', 7: '등록',
                      8: '부팅', 9: '로그온', 11: '세션 상태'}
        triggers = [trig_names.get(t.Type, str(t.Type)) for t in definition.Triggers]
        last = task.LastRunTime
        last_text = ''
        try:
            if last and last.year > 1999:
                last_text = last.strftime('%Y-%m-%d %H:%M')
        except Exception:
            pass
        detail = f'트리거: {", ".join(triggers) or "없음 (다른 곳에서 호출)"}'
        if last_text:
            detail += f' · 마지막 {last_text} {task_result_text(task.LastTaskResult)}'
        highest = definition.Principal.RunLevel == 1
        entries.append(Entry(key=f'task:{name}', source='task', name=name, command=' / '.join(actions),
                             enabled=bool(task.Enabled), running=(task.State == 4), detail=detail,
                             needs_admin=highest))
    return entries


def list_entries() -> list[Entry]:
    """모든 출처의 자동 실행 항목 + 관리 실행기 항목."""
    table = process_table()
    items = load_items()
    adopted = {(it.origin or {}).get('source', '') + ':' + (it.origin or {}).get('name', ''): it.id
               for it in items if it.origin}
    entries: list[Entry] = []

    # 관리 실행기
    last = load_last_run().get('items', {})
    for it in items:
        rec = last.get(it.id)
        detail = f'마지막 {rec["time"]} {rec["result"]}' if rec else '아직 실행 기록 없음'
        opts = []
        if it.delay:
            opts.append(f'{it.delay}초 기다린 뒤 켬')
        if it.window == 'minimized':
            opts.append('최소화')
        if it.restart:
            opts.append(f'꺼지면 다시 켬({interval_text(it.watch_interval())}마다 확인)')
        if it.check_path:
            opts.append(f'{it.check_path} 확인')
        if rec and str(rec.get('result', '')).startswith(STOPPED_PREFIX):
            detail = f'{rec["result"]} · {rec["time"]}'  # 경고는 잘리지 않게 맨 앞에
        elif opts:
            detail = '[' + ', '.join(opts) + '] ' + detail
        proc = it.process_name()
        try:
            running = item_running(it, table)
        except Exception:
            running = None
        entries.append(Entry(key=f'managed:{it.id}', source='managed', name=it.name, command=it.describe(),
                             enabled=it.enabled, running=running,
                             detail=detail, process=proc, managed_id=it.id))

    # 레지스트리 Run
    for source in ('run_hkcu', 'run_hklm', 'run_hklm32'):
        root, subkey, approved_sub = _SOURCE_REG[source]
        approved = _approved_values(root, approved_sub)
        for name, command in _run_values(root, subkey).items():
            exe, _ = split_command(os.path.expandvars(command))
            proc = Path(exe).name
            key = f'{source}:{name}'
            entries.append(Entry(key=key, source=source, name=name, command=command,
                                 enabled=_is_approved(approved.get(name)), running=is_running(proc, table),
                                 process=proc, adopted_by=adopted.get(key, ''),
                                 needs_admin=(root == winreg.HKEY_LOCAL_MACHINE)))

    # 시작프로그램 폴더
    for source, folder in _startup_folders().items():
        root, _, approved_sub = _SOURCE_REG[source]
        approved = _approved_values(root, approved_sub)
        if not folder.exists():
            continue
        for f in sorted(folder.iterdir()):
            if f.name.lower() == 'desktop.ini' or f.is_dir():
                continue
            if f.suffix.lower() == '.lnk':
                target, args, _ = resolve_shortcut(f)
                command = f'"{target}" {args}'.strip()
            else:
                target, command = str(f), str(f)
            proc = Path(target).name if target.lower().endswith('.exe') else ''
            key = f'{source}:{f.name}'
            entries.append(Entry(key=key, source=source, name=f.name, command=command,
                                 enabled=_is_approved(approved.get(f.name)),
                                 running=is_running(proc, table) if proc else None, process=proc,
                                 adopted_by=adopted.get(key, ''),
                                 needs_admin=(root == winreg.HKEY_LOCAL_MACHINE)))

    # 작업 스케줄러 (루트 폴더)
    try:
        entries.extend(_task_entries(table))
    except Exception as e:  # 서비스 문제로 목록 전체가 죽지 않게
        entries.append(Entry(key='task:?', source='task', name='(작업 스케줄러를 읽지 못함)', command=str(e),
                             enabled=False, running=None))
    return entries


# ───────────────────────── 켜기 / 끄기 ─────────────────────────

def _approved_bytes(enabled: bool) -> bytes:
    if enabled:
        return bytes([2]) + bytes(11)
    filetime = int((time.time() + 11644473600) * 10_000_000)
    return bytes([3, 0, 0, 0]) + struct.pack('<Q', filetime)


def _set_approved(source: str, name: str, enabled: bool) -> None:
    root, _, approved_sub = _SOURCE_REG[source]
    data = _approved_bytes(enabled)
    path = f'{APPROVED}\\{approved_sub}'
    if root == winreg.HKEY_LOCAL_MACHINE and not is_admin():
        params = f'add "HKLM\\{path}" /v "{name}" /t REG_BINARY /d {data.hex().upper()} /f'
        code = run_elevated('reg.exe', params)
        if code != 0:
            raise RuntimeError(f'관리자 권한 변경 실패 (코드 {code}). UAC 확인 창에서 취소했을 수 있습니다.')
        return
    key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, data)
    winreg.CloseKey(key)


def _set_task_enabled(name: str, enabled: bool) -> None:
    svc = _task_service()
    task = svc.GetFolder('\\').GetTask(name)
    try:
        task.Enabled = enabled
    except pywintypes.com_error:
        flag = '/enable' if enabled else '/disable'
        code = run_elevated('schtasks.exe', f'/change /tn "\\{name}" {flag}')
        if code != 0:
            raise RuntimeError(f'작업 변경 실패 (코드 {code}). UAC 확인 창에서 취소했을 수 있습니다.')


def set_enabled(entry: Entry, enabled: bool) -> None:
    """켜기/끄기. 레지스트리·폴더 항목은 값을 지우지 않고 StartupApproved 만 바꾼다(작업 관리자와 같은 방식)."""
    if entry.source == 'managed':
        items = load_items()
        for it in items:
            if it.id == entry.managed_id:
                it.enabled = enabled
        save_items(items)
    elif entry.source == 'task':
        _set_task_enabled(entry.name, enabled)
    else:
        _set_approved(entry.source, entry.name, enabled)


# ───────────────────────── 관리 실행기로 옮기기 / 되돌리기 ─────────────────────────

def item_from_entry(entry: Entry) -> Item:
    """Run·폴더 항목으로 관리 항목을 만들어 돌려준다(저장·변경 없음)."""
    if entry.source in ('managed', 'task'):
        raise ValueError('레지스트리 Run 이나 시작프로그램 폴더 항목만 옮길 수 있습니다.')
    if entry.source.startswith('folder'):
        folder = _startup_folders()[entry.source]
        target, args, workdir = resolve_shortcut(folder / entry.name)
        name = Path(entry.name).stem
    else:
        target, args = split_command(os.path.expandvars(entry.command))
        workdir = ''
        name = entry.name
    return Item(name=name, type='exe', path=target, args=args, workdir=workdir,
                enabled=entry.enabled, origin={'source': entry.source, 'name': entry.name})


def adopt(entry: Entry, item: Item | None = None) -> Item:
    """Run·폴더 항목을 관리 실행기로 옮긴다: 관리 항목(item, 없으면 기본값)을 저장하고 원래 항목은 끈다(값은 남김)."""
    if entry.adopted_by:
        raise ValueError('이미 시작 관리자가 켜도록 옮긴 항목입니다.')
    if item is None:
        item = item_from_entry(entry)
    item.origin = {'source': entry.source, 'name': entry.name}
    if entry.enabled:
        set_enabled(entry, False)
    items = load_items()
    items.append(item)
    save_items(items)
    return item


def release(item_id: str) -> None:
    """관리 항목을 지운다. 다른 출처에서 옮겨 온 것이면 원래 항목을 다시 켠다."""
    items = load_items()
    target = next((it for it in items if it.id == item_id), None)
    if target is None:
        return
    if target.origin and target.enabled:
        src, name = target.origin['source'], target.origin['name']
        if src in _SOURCE_REG:
            _set_approved(src, name, True)
    save_items([it for it in items if it.id != item_id])


# ───────────────────────── 목록에서 삭제 ─────────────────────────

BACKUP_DIR = DATA_DIR / 'deleted'


def _backup(name: str, data: dict | str, suffix: str = '.json') -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name)
    path = BACKUP_DIR / f'{dt.datetime.now():%Y%m%d-%H%M%S}_{safe}{suffix}'
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(text, encoding='utf-8')
    return path


def _recycle(path: Path) -> None:
    """휴지통으로 보낸다 (영구 삭제 안 함)."""
    from win32com.shell import shell as _sh
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI
    res, aborted = _sh.SHFileOperation((0, shellcon.FO_DELETE, str(path), None, flags, None, None))
    if res != 0 or aborted:
        raise RuntimeError(f'휴지통으로 보내지 못함 (코드 {res})')


def _delete_value(root, subkey: str, name: str) -> None:
    """레지스트리 값 하나 삭제. HKLM 이고 관리자가 아니면 UAC 로 reg.exe 실행."""
    if root == winreg.HKEY_LOCAL_MACHINE and not is_admin():
        code = run_elevated('reg.exe', f'delete "HKLM\\{subkey}" /v "{name}" /f')
        if code not in (0, 1):  # 1 = 값이 원래 없음
            raise RuntimeError(f'관리자 권한 삭제 실패 (코드 {code}). UAC 확인 창에서 취소했을 수 있습니다.')
        return
    try:
        key = winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        return
    try:
        winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass
    finally:
        winreg.CloseKey(key)


def delete_entry(entry: Entry) -> str:
    """항목을 목록에서 아예 지운다. 지우기 전 내용을 DATA_DIR\\deleted 에 백업하고, 파일은 휴지통으로 보낸다.
    관리 항목이면 옮겨 오기 전 원래 항목(꺼 둔 Run 값·바로 가기)도 같이 지운다. 백업 위치 문구를 돌려준다."""
    if entry.source == 'task' and entry.name == LAUNCHER_TASK:
        raise ValueError('시작 관리자 자신의 로그온 작업은 "로그온 때 자동 실행 끄기"로 지워 주세요.')
    if entry.source == 'managed':
        items = load_items()
        item = next((it for it in items if it.id == entry.managed_id), None)
        if item is None:
            return ''
        if item.name == SELF_ITEM_NAME:
            raise ValueError('시작 관리자 자신의 항목은 지울 수 없습니다. 끄기만 해 주세요.')
        backup = _backup(f'managed-{item.name}', item.__dict__)
        if item.origin and item.origin.get('source') in _SOURCE_REG:
            src, name = item.origin['source'], item.origin['name']
            origin = next((e for e in list_entries() if e.key == f'{src}:{name}'), None)
            if origin is not None:
                origin.adopted_by = ''
                delete_entry(origin)
        save_items([it for it in items if it.id != item.id])
        log(f'목록에서 삭제: 관리 항목 "{item.name}" (백업 {backup})')
        return str(backup)

    if entry.source == 'task':
        svc = _task_service()
        task = svc.GetFolder('\\').GetTask(entry.name)
        backup = _backup(f'task-{entry.name}', task.Xml, '.xml')
        try:
            svc.GetFolder('\\').DeleteTask(entry.name, 0)
        except pywintypes.com_error:
            code = run_elevated('schtasks.exe', f'/delete /tn "\\{entry.name}" /f')
            if code != 0:
                raise RuntimeError(f'작업 삭제 실패 (코드 {code}). UAC 확인 창에서 취소했을 수 있습니다.')
        log(f'목록에서 삭제: 작업 "{entry.name}" (백업 {backup}, 다시 만들려면 작업 스케줄러에서 가져오기)')
        return str(backup)

    root, subkey, approved_sub = _SOURCE_REG[entry.source]
    hive = 'HKLM' if root == winreg.HKEY_LOCAL_MACHINE else 'HKCU'
    if subkey:  # 레지스트리 Run
        backup = _backup(f'run-{entry.name}', {'hive': hive, 'key': subkey, 'name': entry.name,
                                               'value': entry.command, 'enabled': entry.enabled})
        _delete_value(root, subkey, entry.name)
    else:  # 시작프로그램 폴더
        path = _startup_folders()[entry.source] / entry.name
        backup = _backup(f'folder-{entry.name}', {'path': str(path), 'command': entry.command,
                                                  'note': '바로 가기 파일은 휴지통에 있음'})
        if path.exists():
            if root == winreg.HKEY_LOCAL_MACHINE and not is_admin():
                ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::"
                      f"DeleteFile('{str(path).replace(chr(39), chr(39) * 2)}','OnlyErrorDialogs','SendToRecycleBin')")
                code = run_elevated('powershell.exe', f'-NoProfile -Command "{ps}"')
                if code != 0:
                    raise RuntimeError(f'바로 가기 삭제 실패 (코드 {code}).')
            else:
                _recycle(path)
    # 켜짐/꺼짐 기록(StartupApproved)도 정리. 남아도 무해하므로 UAC 창을 또 띄우지는 않는다.
    if root == winreg.HKEY_CURRENT_USER or is_admin():
        try:
            _delete_value(root, f'{APPROVED}\\{approved_sub}', entry.name)
        except Exception:
            pass
    log(f'목록에서 삭제: {entry.source_label} "{entry.name}" (백업 {backup})')
    return str(backup)


def update_item(item: Item) -> None:
    items = load_items()
    for i, it in enumerate(items):
        if it.id == item.id:
            items[i] = item
            break
    else:
        items.append(item)
    save_items(items)


def move_item(item_id: str, offset: int) -> None:
    items = load_items()
    idx = next((i for i, it in enumerate(items) if it.id == item_id), None)
    if idx is None:
        return
    new = max(0, min(len(items) - 1, idx + offset))
    items.insert(new, items.pop(idx))
    save_items(items)


# ───────────────────────── 실행 ─────────────────────────

def steam_exe() -> str:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
        value, _ = winreg.QueryValueEx(key, 'SteamExe')
        winreg.CloseKey(key)
        return value.replace('/', '\\')
    except OSError:
        return r'C:\Program Files (x86)\Steam\steam.exe'


def steam_ready() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess')
        pid, _ = winreg.QueryValueEx(key, 'pid')
        user, _ = winreg.QueryValueEx(key, 'ActiveUser')
        winreg.CloseKey(key)
    except OSError:
        return False
    if not pid or not user:
        return False
    try:
        h = win32api.OpenProcess(0x1000, False, pid)
        win32api.CloseHandle(h)
        return True
    except pywintypes.error:
        return False


def wait_steam(max_seconds: int = 300) -> bool:
    start = time.time()
    kicked = False
    while not steam_ready():
        elapsed = time.time() - start
        if elapsed >= max_seconds:
            log(f'Steam 로그인을 {max_seconds}초 기다렸지만 끝나지 않음')
            return False
        if not kicked and elapsed >= 60 and not is_running('steam.exe'):
            log('Steam 이 안 켜져 있어 직접 켬')
            _shell_open(steam_exe(), '-silent', '', win32con.SW_SHOWMINNOACTIVE)
            kicked = True
        time.sleep(2)
    if time.time() - start > 1:
        log(f'Steam 준비 완료 ({time.time() - start:.0f}초 대기)')
        time.sleep(5)  # 막 로그인한 Steam 이 요청을 놓치지 않게
    return True


def _shell_open(path: str, args: str, workdir: str, show: int) -> None:
    shell.ShellExecuteEx(fMask=0x100 | 0x400,
                         lpFile=path, lpParameters=args, lpDirectory=workdir or '', nShow=show)


def _wait_item(item: Item, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if find_pids(item):
            return True
        time.sleep(1)
    return False


def launch_item(item: Item, watchers: list[threading.Thread] | None = None) -> str:
    """항목 하나를 켠다. 결과 문구를 돌려준다. 이미 켜져 있으면 다시 켜지 않는다."""
    proc = item.process_name()
    if proc and find_pids(item):
        log(f'{item.name}: 이미 실행 중이라 건너뜀')
        return '이미 실행 중 (건너뜀)'

    minimized = item.window == 'minimized'
    result = ''
    if item.type == 'exe':
        if not Path(item.path).exists():
            log(f'{item.name}: 실행 파일이 없음 ({item.path})')
            return '실패: 실행 파일 없음'
        show = win32con.SW_SHOWMINNOACTIVE if minimized else win32con.SW_SHOWNORMAL
        workdir = item.workdir or str(Path(item.path).parent)
        try:
            _shell_open(item.path, item.args, workdir, show)
        except pywintypes.error as e:
            log(f'{item.name}: 실행 실패 ({e.strerror})')
            return f'실패: {e.strerror}'
        log(f'{item.name}: 켬')
        result = '켬'
    elif item.type == 'task':
        try:
            _task_service().GetFolder('\\').GetTask(item.task).Run(None)
        except pywintypes.com_error as e:
            log(f'{item.name}: 작업 "{item.task}" 실행 실패 ({e})')
            return '실패: 작업 실행 안 됨'
        log(f'{item.name}: 작업 "{item.task}" 실행')
        result = '켬'
    elif item.type == 'steam':
        if not wait_steam():
            return '실패: Steam 준비 안 됨'
        if proc and find_pids(item):
            log(f'{item.name}: 이미 실행 중이라 건너뜀')
            return '이미 실행 중 (건너뜀)'
        ok = False
        for attempt in (1, 2):
            log(f'{item.name}: 실행 요청 ({attempt}번째)')
            _shell_open(steam_exe(), f'steam://rungameid/{item.appid}', '', win32con.SW_SHOWNORMAL)
            if not proc or _wait_item(item, 60):
                ok = True
                break
        if not ok:
            log(f'{item.name}: 두 번 요청했지만 안 켜짐')
            return '실패: 두 번 요청했지만 안 켜짐'
        log(f'{item.name}: 켜짐')
        result = '켬'
    else:
        return f'실패: 알 수 없는 종류 {item.type}'

    if minimized and proc:
        t = threading.Thread(target=keep_minimized, args=(item, item.name), daemon=False)
        t.start()
        if watchers is not None:
            watchers.append(t)
        result += ' (최소화)'
    return result


def run_all() -> None:
    """로그온 때: 켜진 관리 항목을 순서대로 지연을 지키며 켠다."""
    import win32event
    mutex = win32event.CreateMutex(None, False, 'Local\\StartupManagerLauncher')
    if win32api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log('실행기가 이미 돌고 있어 이번 실행은 그만둠')
        return
    log('── 로그온 실행 시작 ──')
    record = {'started': now_text(), 'finished': '', 'items': load_last_run().get('items', {})}
    watchers: list[threading.Thread] = []
    for item in load_items():
        if not item.enabled:
            continue
        if item.delay > 0:
            time.sleep(item.delay)
        try:
            result = launch_item(item, watchers)
        except Exception as e:  # 한 항목 실패가 나머지를 막지 않게
            log(f'{item.name}: 예외 {e!r}')
            result = f'실패: {e}'
        record['items'][item.id] = {'time': now_text(), 'result': result}
        _save_last_run(record)
        time.sleep(2)
    for t in watchers:
        t.join()
    record['finished'] = now_text()
    _save_last_run(record)
    log(f'── 로그온 실행 끝, 감시 시작 (꺼지면 다시 켜기 항목을 항목별 주기(기본 {WATCH_INTERVAL}초)로 확인) ──')
    watch_loop(record)
    del mutex


WATCH_INTERVAL = 60          # 기본 감시 주기 (초). 항목마다 check_every 로 바꿀 수 있음
WATCH_TICK = 5               # 감시 루프가 깨어나는 간격 (초)
RESTART_LIMIT = 5            # RESTART_WINDOW 안에 이만큼 다시 켜면
RESTART_WINDOW = 600         # (초)
RESTART_PAUSE = 1800         # 이 시간 동안 다시 켜기를 쉰다 (계속 죽는 프로그램 무한 재시작 방지)
VERIFY_WAITS = (30, 60, 120)  # 다시 켠 뒤 떴는지 확인하는 간격 (초). 끝까지 안 보이면 감시를 멈춤
STOPPED_PREFIX = '⚠ 감시 멈춤'


def _item_sig(item: Item) -> str:
    return json.dumps(item.__dict__, ensure_ascii=False, sort_keys=True)


def watch_loop(record: dict) -> None:
    """'꺼지면 다시 켜기' 항목을 주기적으로 확인한다. 설정 파일을 매번 다시 읽으므로 GUI 에서 바꾸면 바로 반영된다."""
    state: dict[str, dict] = {}
    while True:
        try:
            items = [it for it in load_items() if it.enabled and it.restart]
        except Exception as e:  # 설정 파일을 저장하는 순간 등
            log(f'감시: 설정을 못 읽음 {e!r}')
            items = []
        now = time.time()
        for item in items:
            st = state.setdefault(item.id, {'path_fail': 0, 'restarts': [], 'paused_until': 0.0,
                                            'last_check': 0.0, 'verify': None, 'stopped': ''})
            sig = _item_sig(item)
            if st['stopped']:
                if st['stopped'] == sig:
                    # 감시 멈춤: 설정을 고쳐 저장하면 다시 시작. 경로 때문에 멈췄으면 경로가 돌아올 때도 다시 시작
                    if not st.get('stopped_path') or now - st['last_check'] < item.watch_interval() - WATCH_TICK:
                        continue
                    st['last_check'] = now
                    if not path_ok(item.check_path):
                        continue
                    log(f'{item.name}: {item.check_path} 가 다시 보여 감시를 다시 시작')
                    record['items'][item.id] = {'time': now_text(), 'result': f'{item.check_path} 돌아와 감시 다시 시작'}
                else:
                    log(f'{item.name}: 설정이 바뀌어 감시를 다시 시작')
                    record['items'][item.id] = {'time': now_text(), 'result': '설정이 바뀌어 감시 다시 시작'}
                st.update(stopped='', stopped_path=False, verify=None, restarts=[], paused_until=0.0,
                          last_check=0.0, path_fail=0)
                continue
            if st['verify']:
                try:
                    result = _verify_one(item, st, now, sig)
                except Exception as e:
                    log(f'{item.name}: 확인 중 예외 {e!r}')
                    result = ''
                if result:
                    record['items'][item.id] = {'time': now_text(), 'result': result}
                continue
            if now < st['paused_until']:
                continue
            if now - st['last_check'] < item.watch_interval() - WATCH_TICK:  # 루프 지연 감안해 약간 여유
                continue
            st['last_check'] = now
            try:
                result = _watch_one(item, st)
            except Exception as e:
                log(f'{item.name}: 감시 중 예외 {e!r}')
                result = ''
            if result:
                record['items'][item.id] = {'time': now_text(), 'result': result}
            if result.startswith('다시 켬: 켬'):
                # 켠 뒤 정말 떴는지 30초 → 1분 → 2분 뒤에 확인 (판단이 틀려 헛도는 것 방지)
                st['verify'] = {'step': 0, 'at': time.time() + VERIFY_WAITS[0],
                                'need_path': st.pop('relaunch_reason', '') == '경로 없음'}
        record['watching'] = now_text()
        try:
            _save_last_run(record)
        except Exception:
            pass
        time.sleep(WATCH_TICK)


def _verify_one(item: Item, st: dict, now: float, sig: str) -> str:
    v = st['verify']
    if now < v['at']:
        return ''
    pids = find_pids(item)
    path_back = (not v.get('need_path')) or path_ok(item.check_path)
    if pids and path_back:
        st['verify'] = None
        st['last_check'] = now
        return ''
    v['step'] += 1
    if v['step'] < len(VERIFY_WAITS):
        wait = VERIFY_WAITS[v['step']]
        what = '안 보임' if not pids else f'{item.check_path} 가 아직 안 보임'
        log(f'{item.name}: 다시 켰는데 {what}, {interval_text(wait)} 뒤 다시 확인')
        v['at'] = now + wait
        return ''
    st['verify'] = None
    st['stopped'] = sig
    total = interval_text(sum(VERIFY_WAITS))
    if not pids:
        log(f'{item.name}: 다시 켜고 {total} 넘게 기다렸지만 안 보여 감시를 멈춤 '
            f'(이미 켜져 있는데 못 알아보는 것일 수 있음. 프로세스 이름·명령줄 확인 설정을 확인하세요)')
        return f'{STOPPED_PREFIX}: 다시 켰는데도 안 보임 (프로세스 이름·명령줄 확인 설정 확인 필요)'
    st['stopped_path'] = True
    st['last_check'] = now
    log(f'{item.name}: 다시 켜고 {total} 넘게 기다렸지만 {item.check_path} 가 돌아오지 않아 감시를 멈춤 '
        f'(다시 켜서 해결되는 문제가 아닌 것 같음. 네트워크·드라이브 상태를 확인하세요. 경로가 돌아오면 감시를 다시 시작함)')
    return f'{STOPPED_PREFIX}: 다시 켜도 {item.check_path} 가 안 돌아옴 (네트워크·드라이브 확인 필요)'


def _watch_one(item: Item, st: dict) -> str:
    if not item.process_name():
        return ''  # 프로세스를 알 수 없으면 살아 있는지 판단 못 함
    pids = find_pids(item)
    ok_path = path_ok(item.check_path)
    if pids and ok_path:
        st['path_fail'] = 0
        return ''
    if pids and not ok_path:
        st['path_fail'] += 1
        if st['path_fail'] < 2:  # 한 번은 봐준다 (잠깐 끊긴 경우)
            log(f'{item.name}: 실행 중인데 {item.check_path} 가 안 보임 (다음 확인 때도 그러면 다시 켬)')
            return ''
        log(f'{item.name}: {item.check_path} 가 계속 안 보여 끄고 다시 켬')
        failed = terminate_pids(pids)
        if failed:
            log(f'{item.name}: 끄지 못함 (pid {failed}, 권한 부족일 수 있음)')
            return '실패: 끄지 못함'
        time.sleep(3)
        reason = '경로 없음'
    else:
        reason = '꺼져 있음'
    st['path_fail'] = 0
    now = time.time()
    st['restarts'] = [t for t in st['restarts'] if now - t < RESTART_WINDOW] + [now]
    if len(st['restarts']) > RESTART_LIMIT:
        st['paused_until'] = now + RESTART_PAUSE
        st['restarts'] = []
        log(f'{item.name}: {RESTART_WINDOW // 60}분 동안 {RESTART_LIMIT}번 넘게 다시 켜서 '
            f'{RESTART_PAUSE // 60}분 쉼 (계속 죽는 것 같음)')
        return f'다시 켜기 잠시 멈춤 ({RESTART_PAUSE // 60}분)'
    log(f'{item.name}: {reason} → 다시 켬')
    st['relaunch_reason'] = reason
    return '다시 켬: ' + launch_item(item)


# ───────────────────────── 로그온 작업 등록 ─────────────────────────

def pythonw_path() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name('pythonw.exe')
    return str(cand if cand.exists() else exe)


def launcher_command() -> tuple[str, str]:
    """로그온 작업이 실행해야 할 (프로그램, 인수). 지금 이 프로그램 위치 기준."""
    if FROZEN:
        return sys.executable, '--launch'
    return pythonw_path(), f'"{APP_DIR / "launcher.py"}"'


def _norm_cmd(path: str, args: str) -> str:
    return (os.path.normcase(os.path.normpath(path.strip().strip('"'))) + '|' + ' '.join(args.split())).lower()


def launcher_task_info() -> dict | None:
    try:
        task = _task_service().GetFolder('\\').GetTask(LAUNCHER_TASK)
    except pywintypes.com_error:
        return None
    path_ok_ = True
    task_cmd = ''
    want_path, want_args = launcher_command()
    try:  # 작업에 적힌 경로가 지금 프로그램 위치와 같은지 (폴더를 옮겼으면 다름)
        want = _norm_cmd(want_path, want_args)
        acts = [a for a in task.Definition.Actions if a.Type == 0]
        path_ok_ = any(_norm_cmd(a.Path or '', a.Arguments or '') == want for a in acts)
        if acts:
            task_cmd = _launcher_dir(acts[0].Path or '', acts[0].Arguments or '')
    except Exception:
        pass
    return {'enabled': bool(task.Enabled), 'last': str(task.LastRunTime),
            'result': task_result_text(task.LastTaskResult), 'running': task.State == 4,
            'watching': load_last_run().get('watching', ''), 'path_ok': path_ok_,
            'task_dir': task_cmd, 'want_dir': _launcher_dir(want_path, want_args)}


def _launcher_dir(path: str, args: str) -> str:
    """로그온 작업 명령에서 '시작 관리자가 있는 폴더'를 뽑는다 (pythonw 면 스크립트 폴더, exe 면 exe 폴더)."""
    path = path.strip().strip('"')
    if Path(path).name.lower() in PYTHON_EXES and args.strip():
        script = args.strip().split('"')[1] if args.strip().startswith('"') else args.split()[0]
        return str(Path(script).parent)
    return str(Path(path).parent)


def register_launcher() -> None:
    """현재 사용자 로그온 때 launcher.py 를 켜는 작업을 만든다(일반 권한)."""
    svc = _task_service()
    td = svc.NewTask(0)
    td.RegistrationInfo.Description = '시작 관리자: 로그온 때 관리 항목을 순서·지연·최소화 옵션대로 켬'
    user = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
    trig = td.Triggers.Create(9)  # 로그온
    trig.UserId = user
    td.Principal.UserId = user
    td.Principal.LogonType = 3  # 대화형
    td.Principal.RunLevel = 0
    td.Settings.DisallowStartIfOnBatteries = False
    td.Settings.StopIfGoingOnBatteries = False
    td.Settings.ExecutionTimeLimit = 'PT0S'  # 감시를 계속하므로 시간 제한 없음
    td.Settings.MultipleInstances = 2  # 이미 돌면 새로 안 켬
    action = td.Actions.Create(0)
    action.Path, action.Arguments = launcher_command()
    action.WorkingDirectory = str(APP_DIR)
    svc.GetFolder('\\').RegisterTaskDefinition(LAUNCHER_TASK, td, 6, None, None, 3)
    ensure_self_item()


def ensure_self_item() -> None:
    """관리 항목 맨 앞에 '시작 관리자 (트레이)'를 두고, 프로그램 위치가 바뀌었으면 경로를 고친다."""
    if FROZEN:
        path, args = sys.executable, '--tray'
    else:
        path, args = pythonw_path(), f'"{APP_DIR / "gui.py"}" --tray'
    items = load_items()
    me = next((it for it in items if it.name == SELF_ITEM_NAME), None)
    if me is None:
        items.insert(0, Item(name=SELF_ITEM_NAME, type='exe', path=path, args=args,
                             workdir=str(APP_DIR), process='-'))
    else:
        me.type, me.path, me.args, me.workdir, me.process = 'exe', path, args, str(APP_DIR), '-'
    save_items(items)


def unregister_launcher() -> None:
    try:
        _task_service().GetFolder('\\').DeleteTask(LAUNCHER_TASK, 0)
    except pywintypes.com_error:
        pass
