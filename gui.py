"""시작 관리자 - PyQt6 GUI.  실행: pythonw gui.py   (자체 점검: python gui.py --selftest)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStackedWidget, QSystemTrayIcon, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

import core

APP_TITLE = '시작 관리자'
SERVER_NAME = 'StartupManagerGuiSingleInstance'
ROLE_ENTRY = Qt.ItemDataRole.UserRole
ADOPTABLE_PREFIX = ('run_', 'folder_')


# ───────────────────────── 백그라운드 작업 ─────────────────────────

class Worker(QThread):
    """함수를 다른 스레드에서 실행하고 (결과, 오류 메시지)를 돌려준다."""
    finished_with = pyqtSignal(object, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
            self.finished_with.emit(result, '')
        except Exception as e:  # noqa: BLE001 - 사용자에게 메시지로 보여줌
            msg = str(e) or e.__class__.__name__
            self.finished_with.emit(None, msg)


# ───────────────────────── 화면 크기에 맞추기 ─────────────────────────

def current_screen_geometry():
    """마우스가 있는 모니터(없으면 주 모니터)의 작업 영역(작업 표시줄 제외)."""
    from PyQt6.QtGui import QCursor, QGuiApplication
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def fit_to_screen(widget, w_ratio: float, h_ratio: float, min_w: int, min_h: int,
                  want_w: int | None = None, want_h: int | None = None) -> None:
    """창 크기를 모니터 작업 영역 비율로 정하고 가운데에 놓는다. 화면보다 커지지 않게 한다."""
    geo = current_screen_geometry()
    max_w, max_h = int(geo.width() * 0.95), int(geo.height() * 0.95)
    w = want_w if want_w is not None else int(geo.width() * w_ratio)
    h = want_h if want_h is not None else int(geo.height() * h_ratio)
    w = min(max(w, min(min_w, max_w)), max_w)
    h = min(max(h, min(min_h, max_h)), max_h)
    widget.resize(w, h)
    widget.move(geo.x() + (geo.width() - w) // 2, geo.y() + (geo.height() - h) // 2)
    # 제목 표시줄 높이와 최소 크기는 창이 뜬 뒤에야 정확히 알 수 있으므로, 뜬 직후 한 번 더 가운데로 맞춘다.
    QTimer.singleShot(0, lambda: recenter(widget, geo))


def recenter(widget, geo) -> None:
    f = widget.frameGeometry()
    x = geo.x() + max(0, (geo.width() - f.width()) // 2)
    y = geo.y() + max(0, (geo.height() - f.height()) // 2)
    widget.move(x, y)


def on_some_screen(widget) -> bool:
    from PyQt6.QtGui import QGuiApplication
    frame = widget.frameGeometry()
    return any(s.availableGeometry().intersects(frame) for s in QGuiApplication.screens())


# ───────────────────────── 항목 편집 대화상자 ─────────────────────────

class ItemDialog(QDialog):
    TYPES = [('exe', '실행 파일'), ('steam', 'Steam 게임'), ('task', '작업 스케줄러 작업')]

    def __init__(self, parent=None, item: core.Item | None = None):
        super().__init__(parent)
        self.setWindowTitle('관리 항목 설정' if item else '새 관리 항목')
        self.setMinimumWidth(560)
        self._src = item
        self._result: core.Item | None = None

        self.name_edit = QLineEdit()
        self.type_combo = QComboBox()
        for key, label in self.TYPES:
            self.type_combo.addItem(label, key)

        # 종류별 입력 칸
        self.stack = QStackedWidget()
        # exe
        exe_page = QWidget()
        ef = QFormLayout(exe_page)
        ef.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        browse = QPushButton('찾아보기…')
        browse.clicked.connect(self._browse_path)
        row = QHBoxLayout()
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        ef.addRow('실행 파일', row)
        self.args_edit = QLineEdit()
        ef.addRow('인수', self.args_edit)
        self.workdir_edit = QLineEdit()
        self.workdir_edit.setPlaceholderText('비우면 실행 파일이 있는 폴더')
        wb = QPushButton('찾아보기…')
        wb.clicked.connect(self._browse_dir)
        row2 = QHBoxLayout()
        row2.addWidget(self.workdir_edit, 1)
        row2.addWidget(wb)
        ef.addRow('작업 폴더', row2)
        self.stack.addWidget(exe_page)
        # steam
        steam_page = QWidget()
        sf = QFormLayout(steam_page)
        sf.setContentsMargins(0, 0, 0, 0)
        self.appid_spin = QSpinBox()
        self.appid_spin.setRange(0, 2_147_483_647)
        self.appid_spin.setGroupSeparatorShown(False)
        sf.addRow('Steam AppId', self.appid_spin)
        self.stack.addWidget(steam_page)
        # task
        task_page = QWidget()
        tf = QFormLayout(task_page)
        tf.setContentsMargins(0, 0, 0, 0)
        self.task_edit = QLineEdit()
        self.task_edit.setPlaceholderText('작업 스케줄러 루트 폴더에 있는 작업 이름')
        tf.addRow('작업 이름', self.task_edit)
        self.stack.addWidget(task_page)

        self.process_edit = QLineEdit()
        self.process_edit.setPlaceholderText('중복 실행 확인용, 예: MyApp.exe — 비우면 실행 파일 이름')
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 600)
        self.delay_spin.setSuffix(' 초')
        delay_row = QHBoxLayout()
        delay_row.addWidget(self.delay_spin)
        delay_row.addWidget(QLabel('켜기 전에 기다릴 시간'))
        delay_row.addStretch(1)
        self.window_combo = QComboBox()
        self.window_combo.addItem('보통', 'normal')
        self.window_combo.addItem('최소화', 'minimized')
        self.enabled_check = QCheckBox('사용 (로그온 때 켬)')
        self.enabled_check.setChecked(True)
        self.match_edit = QLineEdit()
        self.match_edit.setPlaceholderText('명령줄에 이 글자가 있는 것만 같은 프로그램으로 봄, 예: my_script.py '
                                           '(python 항목은 비우면 스크립트 이름)')
        self.restart_check = QCheckBox('꺼지면 다시 켜기 (10분에 5번 넘게 죽으면 30분 쉼)')
        self.every_spin = QSpinBox()
        self.every_spin.setRange(60, 3600)
        self.every_spin.setSingleStep(60)
        self.every_spin.setSuffix(' 초')
        self.every_spin.setValue(core.WATCH_INTERVAL)
        every_row = QHBoxLayout()
        every_row.addWidget(self.every_spin)
        every_row.addWidget(QLabel('마다 꺼졌는지 확인 (일부러 잠깐 끄는 프로그램이면 길게)'))
        every_row.addStretch(1)
        self.restart_check.toggled.connect(self.every_spin.setEnabled)
        self.every_spin.setEnabled(False)
        self.check_path_edit = QLineEdit()
        self.check_path_edit.setPlaceholderText(r'예: Z:\  — 실행 중인데 이 경로가 안 보이면 끄고 다시 켬 (다시 켜기 켰을 때)')

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.addRow('이름', self.name_edit)
        form.addRow('종류', self.type_combo)

        note = QLabel('이미 실행 중인 프로그램은 자동으로 건너뜁니다 (중복 실행 방지).')
        note.setStyleSheet('color:#888; font-size:9pt;')
        note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('확인')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('취소')
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        form2 = QFormLayout()
        form2.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form2.addRow('프로세스 이름', self.process_edit)
        form2.addRow('명령줄 확인', self.match_edit)
        form2.addRow('', self.restart_check)
        form2.addRow('확인 주기', every_row)
        form2.addRow('확인할 경로', self.check_path_edit)
        form2.addRow('지연', delay_row)
        form2.addRow('창 상태', self.window_combo)
        form2.addRow('', self.enabled_check)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.stack)
        lay.addLayout(form2)
        lay.addWidget(note)
        lay.addWidget(buttons)

        self.type_combo.currentIndexChanged.connect(self._type_changed)
        if item:
            self._load(item)
        self._type_changed()
        # 내용에 필요한 높이는 지키고, 너비는 화면 너비의 40% 정도로 (화면보다 크지 않게)
        self.adjustSize()
        fit_to_screen(self, 0.40, 0, 560, self.sizeHint().height(), want_h=self.sizeHint().height())

    def _type_changed(self):
        self.stack.setCurrentIndex(self.type_combo.currentIndex())

    def _load(self, it: core.Item):
        self.name_edit.setText(it.name)
        idx = self.type_combo.findData(it.type)
        self.type_combo.setCurrentIndex(max(0, idx))
        self.path_edit.setText(it.path)
        self.args_edit.setText(it.args)
        self.workdir_edit.setText(it.workdir)
        self.appid_spin.setValue(int(it.appid or 0))
        self.task_edit.setText(it.task)
        self.process_edit.setText(it.process)
        self.delay_spin.setValue(int(it.delay or 0))
        self.window_combo.setCurrentIndex(max(0, self.window_combo.findData(it.window)))
        self.enabled_check.setChecked(bool(it.enabled))
        self.match_edit.setText(it.match)
        self.restart_check.setChecked(bool(it.restart))
        self.every_spin.setValue(it.watch_interval())
        self.check_path_edit.setText(it.check_path)

    def _browse_path(self):
        f, _ = QFileDialog.getOpenFileName(self, '실행 파일 선택', self.path_edit.text(),
                                           '실행 파일 (*.exe *.bat *.cmd *.lnk);;모든 파일 (*.*)')
        if f:
            self.path_edit.setText(str(Path(f)))

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, '작업 폴더 선택', self.workdir_edit.text())
        if d:
            self.workdir_edit.setText(str(Path(d)))

    def _accept(self):
        name = self.name_edit.text().strip()
        typ = self.type_combo.currentData()
        if not name:
            return self._warn('이름을 입력해 주세요.', self.name_edit)
        if typ == 'exe' and not self.path_edit.text().strip():
            return self._warn('실행 파일 경로를 입력해 주세요.', self.path_edit)
        if typ == 'steam' and self.appid_spin.value() <= 0:
            return self._warn('Steam AppId 를 입력해 주세요. (1 이상의 숫자)', self.appid_spin)
        if typ == 'task' and not self.task_edit.text().strip():
            return self._warn('작업 스케줄러 작업 이름을 입력해 주세요.', self.task_edit)
        it = self._src or core.Item(name=name)
        it.name = name
        it.type = typ
        it.path = self.path_edit.text().strip().strip('"') if typ == 'exe' else ''
        it.args = self.args_edit.text().strip() if typ == 'exe' else ''
        it.workdir = self.workdir_edit.text().strip() if typ == 'exe' else ''
        it.appid = self.appid_spin.value() if typ == 'steam' else 0
        it.task = self.task_edit.text().strip() if typ == 'task' else ''
        it.process = self.process_edit.text().strip()
        it.delay = self.delay_spin.value()
        it.window = self.window_combo.currentData()
        it.enabled = self.enabled_check.isChecked()
        it.match = self.match_edit.text().strip()
        it.restart = self.restart_check.isChecked()
        every = self.every_spin.value()
        it.check_every = 0 if every == core.WATCH_INTERVAL else every
        it.check_path = self.check_path_edit.text().strip().strip('"')
        if it.restart and not it.process_name():
            return self._warn('꺼지면 다시 켜려면 살아 있는지 확인할 수 있어야 합니다.\n'
                              '"프로세스 이름"(또는 python 항목이면 "명령줄 확인")을 적어 주세요.', self.process_edit)
        self._result = it
        self.accept()

    def _warn(self, text, widget):
        QMessageBox.warning(self, APP_TITLE, text)
        widget.setFocus()

    def item(self) -> core.Item | None:
        return self._result


# ───────────────────────── 메인 창 ─────────────────────────

class MainWindow(QMainWindow):
    refreshed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self._fitted = False     # 처음 보일 때 화면 크기에 맞춘다 (그 뒤엔 사용자가 바꾼 크기 유지)
        self._workers: set[Worker] = set()
        self._busy = 0           # 진행 중인 변경 작업 수
        self._refreshing = False
        self._entries: list[core.Entry] = []
        self._quitting = False
        self._balloon_shown = False
        self._build_ui()
        self._build_tray()
        self.refresh_launcher_status()
        self.refresh_log()
        self.refresh(initial=True)
        self.timer = QTimer(self)
        self.timer.setInterval(10000)  # 매번 작업 스케줄러·레지스트리·프로세스를 다 읽으므로 너무 잦지 않게
        self.timer.timeout.connect(self._auto_refresh)
        self.timer.start()

    # ── 트레이 ──
    @staticmethod
    def make_icon() -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor('#2f6fd6'))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 14, 14)
        p.setBrush(QColor('#ffffff'))
        p.drawPolygon(*[QPoint(x, y) for x, y in ((24, 17), (24, 47), (48, 32))])
        p.end()
        return QIcon(pm)

    def _build_tray(self):
        self.setWindowIcon(self.make_icon())
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.make_icon(), self)
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu(self)
        a_open = menu.addAction('열기')
        a_open.triggered.connect(self.bring_to_front)
        a_ref = menu.addAction('새로 고침')
        a_ref.triggered.connect(self.on_refresh_clicked)
        menu.addSeparator()
        a_quit = menu.addAction('종료')
        a_quit.triggered.connect(self.quit_app)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.bring_to_front()

    def quit_app(self):
        self._quitting = True
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def showEvent(self, ev):
        # 처음 띄울 때, 또는 모니터 구성이 바뀌어 창이 화면 밖에 있을 때 해상도에 맞춰 다시 잡는다.
        if not self._fitted or not on_some_screen(self):
            self.fit_window()
        super().showEvent(ev)

    def fit_window(self):
        fit_to_screen(self, 0.72, 0.78, 900, 600)
        self._fitted = True
        total = max(600, self.width() - 60)
        for i, r in enumerate(self._col_ratios):
            self.tree.setColumnWidth(i, int(total * r))
        h = max(300, self.height() - 200)
        self._split.setSizes([int(h * 0.74), int(h * 0.26)])

    def closeEvent(self, ev):
        if self._quitting or self.tray is None:
            ev.accept()
            QApplication.quit()
            return
        ev.ignore()
        self.hide()  # 트레이로 숨김 (알림은 띄우지 않음)

    def _update_tray_tooltip(self):
        if not self.tray:
            return
        managed = [e for e in self._entries if e.source == 'managed']
        tip = f'{APP_TITLE}\n관리 항목 {len(managed)}개'
        try:
            last = core.load_last_run().get('finished') or core.load_last_run().get('started')
        except Exception:  # noqa: BLE001
            last = ''
        if last:
            tip += f' · 마지막 실행 {last}'
        self.tray.setToolTip(tip)

    # ── UI 구성 ──
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 8)
        root.setSpacing(10)

        # 상단 바
        top = QHBoxLayout()
        self.launcher_label = QLabel('로그온 자동 실행: 확인 중…')
        self.launcher_label.setWordWrap(True)
        self.launcher_label.setStyleSheet('font-weight:600;')
        top.addWidget(self.launcher_label, 1)
        self.btn_register = QPushButton('로그온 때 자동 실행 켜기')
        self.btn_register.clicked.connect(self.on_register)
        self.btn_unregister = QPushButton('로그온 때 자동 실행 끄기')
        self.btn_unregister.clicked.connect(self.on_unregister)
        self.btn_refresh = QPushButton('새로 고침')
        self.btn_refresh.clicked.connect(self.on_refresh_clicked)
        self.btn_folder = QPushButton('설정 폴더')
        self.btn_folder.setToolTip(f'설정·기록 파일이 있는 폴더 열기\n{core.DATA_DIR}')
        self.btn_folder.clicked.connect(lambda: os.startfile(str(core.DATA_DIR)))
        for b in (self.btn_register, self.btn_unregister, self.btn_folder, self.btn_refresh):
            top.addWidget(b)
        root.addLayout(top)

        # 트리
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(['이름', '상태', '실행 중', '설명', '명령'])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(14)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_context_menu)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        self.tree.itemDoubleClicked.connect(self.on_double_click)
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._sort_col: int | None = None   # None = 기본 정렬
        self._sort_desc = False
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(False)
        hdr.setToolTip('머리글을 누르면 정렬: 오름차순 → 내림차순 → 기본(상태 → 실행 중 → 이름)')
        hdr.sectionClicked.connect(self.on_header_clicked)
        self._col_ratios = (0.22, 0.09, 0.09, 0.30)  # 마지막 열(명령)은 남는 폭을 채움
        for i, w in enumerate((260, 130, 90, 300, 300)):
            self.tree.setColumnWidth(i, w)

        # 버튼 줄
        bar = QHBoxLayout()
        self.btn_toggle = QPushButton('켜기/끄기')
        self.btn_adopt = QPushButton('시작 관리자가 켜도록 옮기기')
        self.btn_adopt.setToolTip('Windows 자동 실행은 끄고, 대신 시작 관리자가 로그온 때 켭니다 (순서·지연·최소화 설정 가능)')
        self.btn_settings = QPushButton('설정…')
        self.btn_up = QPushButton('위로')
        self.btn_down = QPushButton('아래로')
        self.btn_run = QPushButton('지금 실행')
        self.btn_release = QPushButton('시작 관리자에서 빼기')
        self.btn_release.setToolTip('시작 관리자가 켜는 목록에서 뺍니다. 옮겨 온 항목이면 원래 Windows 자동 실행이 다시 켜집니다')
        self.btn_delete = QPushButton('목록에서 삭제')
        self.btn_delete.setToolTip('필요 없어진 항목을 아예 지웁니다 (백업을 남기고, 파일은 휴지통으로)')
        self.btn_delete.clicked.connect(self.on_delete)
        self.btn_new = QPushButton('새 관리 항목…')
        self.btn_toggle.clicked.connect(self.on_toggle)
        self.btn_adopt.clicked.connect(self.on_adopt)
        self.btn_settings.clicked.connect(self.on_settings)
        self.btn_up.clicked.connect(lambda: self.on_move(-1))
        self.btn_down.clicked.connect(lambda: self.on_move(1))
        self.btn_run.clicked.connect(self.on_run_now)
        self.btn_release.clicked.connect(self.on_release)
        self.btn_new.clicked.connect(self.on_new)
        for b in (self.btn_toggle, self.btn_adopt, self.btn_settings, self.btn_up, self.btn_down,
                  self.btn_run, self.btn_release, self.btn_delete):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.btn_new)

        upper = QWidget()
        ul = QVBoxLayout(upper)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.setSpacing(8)
        ul.addWidget(self.tree, 1)
        ul.addLayout(bar)

        # 실행 기록
        self.tabs = QTabWidget()
        log_page = QWidget()
        ll = QVBoxLayout(log_page)
        ll.setContentsMargins(6, 6, 6, 6)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont('Consolas', 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(mono)
        ll.addWidget(self.log_view)
        self.tabs.addTab(log_page, '실행 기록')

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(upper)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([480, 170])
        self._split = split
        root.addWidget(split, 1)

        self.statusBar().showMessage('준비')
        self.update_buttons()

    # ── 상태 표시 ──
    def refresh_launcher_status(self):
        try:
            info = core.launcher_task_info()
        except Exception as e:  # noqa: BLE001
            self.launcher_label.setText(f'로그온 자동 실행: 확인 실패 ({e})')
            self._launcher_registered = None
            self.btn_register.setText('로그온 때 자동 실행 켜기')
            self.btn_register.setVisible(True)
            self.btn_unregister.setVisible(False)
            return
        self._launcher_registered = info is not None
        self._launcher_path_ok = bool(info is None or info.get('path_ok', True))
        if info is None:
            self.launcher_label.setText('로그온 자동 실행: 꺼짐 (로그온 때 아무것도 켜지 않음)')
        else:
            last = (info.get('last') or '').split('+')[0].split('.')[0].strip()
            if not last or last.startswith(('1899', '1999', '1601', '1900')):
                last_text = '아직 실행 안 됨'
            else:
                last_text = f'{last} ({info.get("result", "")})'
            state = '켜짐' if info.get('enabled') else '꺼짐 (작업 스케줄러에서 사용 안 함)'
            text = f'로그온 자동 실행: {state} · 마지막 실행 {last_text}'
            if info.get('running'):
                text += f' · 감시 중 (마지막 확인 {info.get("watching", "")[-8:]})'
            if not self._launcher_path_ok:
                text += '\n⚠ 작업 스케줄러에 적힌 프로그램 위치가 지금 위치와 다릅니다 (폴더를 옮겼나요?)'
            self.launcher_label.setText(text)
        # 켜기 버튼은 꺼져 있을 때만, 위치가 어긋났을 때는 '고치기'로 보인다. 끄기 버튼은 켜져 있을 때만.
        enabled = bool(info and info.get('enabled'))
        if not self._launcher_registered or not enabled:
            self.btn_register.setText('로그온 때 자동 실행 켜기')
            self.btn_register.setVisible(True)
        elif not self._launcher_path_ok:
            self.btn_register.setText('자동 실행 경로를 지금 위치로 고치기')
            self.btn_register.setVisible(True)
        else:
            self.btn_register.setVisible(False)
        self.btn_register.setEnabled(True)
        self.btn_unregister.setVisible(bool(self._launcher_registered))
        self.btn_unregister.setEnabled(bool(self._launcher_registered))

    def refresh_log(self):
        try:
            text = core.read_log_tail()
        except Exception as e:  # noqa: BLE001
            text = f'(기록을 읽지 못함: {e})'
        self.log_view.setPlainText(text or '(아직 실행 기록이 없습니다)')
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ── 목록 갱신 ──
    def _modal_open(self) -> bool:
        return QApplication.activeModalWidget() is not None

    def _auto_refresh(self):
        if self._modal_open() or self._busy or not self.isVisible():
            return
        self.refresh()

    def on_refresh_clicked(self):
        self.refresh_launcher_status()
        self.refresh_log()
        self.refresh()

    def refresh(self, initial: bool = False, keep_key: str | None = None):
        if self._refreshing:
            return
        self._refreshing = True
        if initial:
            self.statusBar().showMessage('목록을 읽는 중…')
        w = Worker(core.list_entries, self)
        self._workers.add(w)

        def done(result, err):
            self._workers.discard(w)
            self._refreshing = False
            if err:
                self.statusBar().showMessage(f'목록을 읽지 못함: {err}')
            else:
                self._populate(result, keep_key)
                self.statusBar().showMessage(f'항목 {len(result)}개 · 갱신 {core.now_text()}')
            w.deleteLater()
            self.refreshed.emit()

        w.finished_with.connect(done)
        w.start()

    def selected_entry(self) -> core.Entry | None:
        it = self.tree.currentItem()
        if it is None or not it.isSelected():
            return None
        return it.data(0, ROLE_ENTRY)

    # ── 정렬 ──
    @staticmethod
    def _state_rank(e: core.Entry) -> int:
        return 1 if e.adopted_by else (0 if e.enabled else 2)       # 켜짐 → 옮김 → 꺼짐

    @staticmethod
    def _run_rank(e: core.Entry) -> int:
        return 0 if e.running else (1 if e.running is None else 2)  # 실행 중 → ? → 꺼짐

    def _default_key(self, e: core.Entry):
        return (self._state_rank(e), self._run_rank(e), e.name.casefold())

    def _sorted(self, es: list[core.Entry], keep_order: bool = False) -> list[core.Entry]:
        """그룹 안 정렬. 기본: 상태(켜진 것 위) → 실행 중 → 이름(abc·가나다).
        머리글을 누르면 그 열 기준 오름/내림차순, 같은 값끼리는 기본 순서.
        관리 실행기는 켜는 순서가 의미 있으므로 늘 설정 순서 그대로 둔다."""
        if keep_order:
            return list(es)
        result = sorted(es, key=self._default_key)
        col = self._sort_col
        if col is not None:
            keys = {
                0: lambda e: e.name.casefold(),
                1: self._state_rank,
                2: self._run_rank,
                3: lambda e: e.detail.casefold(),
                4: lambda e: e.command.casefold(),
            }
            result.sort(key=keys[col], reverse=self._sort_desc)  # 안정 정렬이라 같은 값은 기본 순서 유지
        return result

    def on_header_clicked(self, col: int):
        # 같은 열을 누를 때마다: 오름차순 → 내림차순 → 기본 정렬
        if self._sort_col != col:
            self._sort_col, self._sort_desc = col, False
        elif not self._sort_desc:
            self._sort_desc = True
        else:
            self._sort_col = None
        hdr = self.tree.header()
        if self._sort_col is None:
            hdr.setSortIndicatorShown(False)
        else:
            hdr.setSortIndicatorShown(True)
            hdr.setSortIndicator(col, Qt.SortOrder.DescendingOrder if self._sort_desc
                                 else Qt.SortOrder.AscendingOrder)
        if self._entries:
            self._populate(self._entries)

    def _populate(self, entries: list[core.Entry], keep_key: str | None = None):
        sel = keep_key or (self.selected_entry().key if self.selected_entry() else None)
        vbar = self.tree.verticalScrollBar().value()
        self._entries = entries
        # 그룹 순서: 관리 실행기 먼저, 나머지는 출처 정의 순서
        order = list(core.SOURCE_LABELS.keys())
        groups: dict[str, list[core.Entry]] = {}
        for e in entries:
            groups.setdefault(e.source, []).append(e)
        sources = sorted(groups, key=lambda s: order.index(s) if s in order else 99)

        grey = QBrush(QColor('#9a9a9a'))
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        select_item = None
        for src in sources:
            es = self._sorted(groups[src], keep_order=(src == 'managed'))
            head = QTreeWidgetItem([f'{es[0].source_label}  ({len(es)})'])
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)
            f = head.font(0)
            f.setBold(True)
            for c in range(5):
                head.setFont(c, f)
                head.setBackground(c, QBrush(QColor('#e8eef7')))
                head.setForeground(c, QBrush(QColor('#23406b')))
            self.tree.addTopLevelItem(head)
            for e in es:
                name = e.name + ('   🛡' if e.needs_admin else '')
                if e.adopted_by:
                    state = '시작 관리자가 켬'
                else:
                    state = '켜짐' if e.enabled else '꺼짐'
                run = '?' if e.running is None else ('● 실행 중' if e.running else '–')
                row = QTreeWidgetItem([name, state, run, e.detail, e.command])
                row.setData(0, ROLE_ENTRY, e)
                tip = e.command
                if e.needs_admin:
                    tip = '관리자 권한 필요 (켜기/끄기 때 확인 창이 뜰 수 있음)\n' + tip
                row.setToolTip(0, tip)
                row.setToolTip(3, e.detail)
                row.setToolTip(4, e.command)
                if e.needs_admin:
                    row.setToolTip(0, '관리자 권한 필요\n' + e.command)
                if e.running:
                    row.setForeground(2, QBrush(QColor('#1a8a3a')))
                inactive = (not e.enabled) or bool(e.adopted_by)
                if inactive:
                    for c in range(5):
                        row.setForeground(c, grey)
                elif e.enabled:
                    row.setForeground(1, QBrush(QColor('#1a6fd0')))
                if core.STOPPED_PREFIX in (e.detail or ''):
                    row.setForeground(3, QBrush(QColor('#c62828')))
                head.addChild(row)
                if sel and e.key == sel:
                    select_item = row
            head.setExpanded(True)
        self.tree.setUpdatesEnabled(True)
        self._update_tray_tooltip()
        if select_item is not None:
            self.tree.setCurrentItem(select_item)
        self.tree.verticalScrollBar().setValue(vbar)
        self.update_buttons()

    # ── 버튼 상태 ──
    def update_buttons(self):
        e = self.selected_entry()
        busy = self._busy > 0
        managed = bool(e and e.source == 'managed')
        adoptable = bool(e and e.source.startswith(ADOPTABLE_PREFIX) and not e.adopted_by)
        self.btn_toggle.setEnabled(bool(e) and not busy)
        if e:
            self.btn_toggle.setText('끄기' if e.enabled else '켜기')
        else:
            self.btn_toggle.setText('켜기/끄기')
        self.btn_adopt.setEnabled(adoptable and not busy)
        for b in (self.btn_up, self.btn_down, self.btn_run, self.btn_release):
            b.setEnabled(managed and not busy)
        deletable = bool(e) and not e.adopted_by and e.key != 'task:?' and not (
            e.source == 'task' and e.name == core.LAUNCHER_TASK) and not (
            e.source == 'managed' and e.name == core.SELF_ITEM_NAME)
        self.btn_delete.setEnabled(deletable and not busy)
        # 설정(최소화·지연 등)은 Run·폴더 항목에도 연다: 저장하면 시작 관리자가 켜도록 옮겨진다.
        self.btn_settings.setEnabled(bool(e and (managed or adoptable or e.adopted_by)) and not busy)
        self.btn_new.setEnabled(not busy)

    # ── 공통 작업 실행 ──
    def run_task(self, fn, on_done, status: str, keep_key: str | None = None, refresh: bool = True):
        self._busy += 1
        self.update_buttons()
        self.statusBar().showMessage(status)
        w = Worker(fn, self)
        self._workers.add(w)

        def done(result, err):
            self._workers.discard(w)
            self._busy -= 1
            w.deleteLater()
            if err:
                QMessageBox.warning(self, APP_TITLE, f'작업에 실패했습니다.\n\n{err}')
                self.statusBar().showMessage('실패: ' + err.splitlines()[0])
            elif on_done:
                on_done(result)
            self.refresh_log()
            self.update_buttons()
            if refresh:
                self.refresh(keep_key=keep_key)

        w.finished_with.connect(done)
        w.start()

    def _find_item(self, item_id: str) -> core.Item | None:
        for it in core.load_items():
            if it.id == item_id:
                return it
        return None

    def _notify(self, text: str, ms: int = 3000) -> None:
        """잠깐 보여 주고 저절로 닫히는 알림. 떠 있는 동안에도 다른 조작을 막지 않는다."""
        box = QMessageBox(QMessageBox.Icon.Information, APP_TITLE, text, QMessageBox.StandardButton.Ok, self)
        box.button(QMessageBox.StandardButton.Ok).setText('확인')
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        t = QTimer(box)  # 알림 창의 자식: 먼저 닫혀도 타이머가 같이 사라짐
        t.setSingleShot(True)
        t.timeout.connect(box.close)
        t.start(ms)
        box.show()

    def _confirm(self, text: str, title: str = APP_TITLE) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.button(QMessageBox.StandardButton.Yes).setText('예')
        box.button(QMessageBox.StandardButton.No).setText('아니요')
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    # ── 상단 바 동작 ──
    def on_register(self):
        fixing = bool(self._launcher_registered) and not getattr(self, '_launcher_path_ok', True)
        if fixing:
            q = ('작업 스케줄러에 적힌 시작 관리자 위치를 지금 위치로 고칠까요?\n'
                 f'({core.APP_DIR})')
        else:
            q = ('로그온 때 시작 관리자가 자동으로 켜지게 할까요?\n'
                 '켜지면 트레이에 뜨고, "시작 관리자가 켜는 항목"을 순서대로 켭니다.\n'
                 '(작업 스케줄러에 일반 권한으로 등록되며 언제든 끌 수 있습니다.)')
        if not self._confirm(q):
            return

        def after(_):
            self.refresh_launcher_status()
            self.statusBar().showMessage('자동 실행 경로를 고쳤습니다.' if fixing else '로그온 때 자동 실행을 켰습니다.')

        self.run_task(core.register_launcher, after, '로그온 자동 실행 설정 중…', refresh=False)

    def on_unregister(self):
        if not self._confirm('로그온 때 자동 실행을 끌까요?\n'
                             '끄면 로그온 때 시작 관리자와 "시작 관리자가 켜는 항목"이 자동으로 켜지지 않습니다.'):
            return

        def after(_):
            self.refresh_launcher_status()
            self.statusBar().showMessage('로그온 때 자동 실행을 껐습니다.')

        self.run_task(core.unregister_launcher, after, '로그온 자동 실행 끄는 중…', refresh=False)

    # ── 행 동작 ──
    def on_toggle(self):
        e = self.selected_entry()
        if not e:
            return
        new = not e.enabled
        status = f'"{e.name}" {"켜는" if new else "끄는"} 중…'
        if e.needs_admin:
            status += ' (관리자 권한 확인 창이 뜰 수 있습니다)'
        self.run_task(lambda: core.set_enabled(e, new), None, status, keep_key=e.key)

    def on_adopt(self):
        e = self.selected_entry()
        if not e or not e.source.startswith(ADOPTABLE_PREFIX) or e.adopted_by:
            return
        if not self._confirm(
                f'"{e.name}" 을(를) 시작 관리자가 켜도록 옮길까요?\n\n'
                '· 원래 Windows 자동 실행 항목은 그대로 남지만 꺼집니다.\n'
                '· 대신 시작 관리자가 이 프로그램을 로그온 때 켭니다 (순서·지연·최소화 설정 가능).\n'
                '· 나중에 "시작 관리자에서 빼기"를 누르면 원래 항목이 다시 켜집니다.'):
            return
        self.run_task(lambda: core.adopt(e), lambda it: self.statusBar().showMessage(
            f'"{e.name}" 을(를) 시작 관리자가 켜도록 옮겼습니다.'), f'"{e.name}" 옮기는 중…',
            keep_key=e.key)

    def on_settings(self):
        e = self.selected_entry()
        if not e:
            return
        if e.source.startswith(ADOPTABLE_PREFIX) and not e.adopted_by:
            # Run·폴더 항목: 설정을 저장하면 관리 실행기로 옮긴다 (Windows 자체 자동 실행엔 창 상태 옵션이 없음).
            try:
                preview = core.item_from_entry(e)
            except Exception as ex:
                QMessageBox.warning(self, APP_TITLE, str(ex))
                return
            dlg = ItemDialog(self, preview)
            dlg.setWindowTitle(f'설정 - {e.name}')
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            new = dlg.item()
            if not self._confirm(
                    f'"{e.name}" 설정을 적용하려면 시작 관리자가 켜도록 옮겨야 합니다.\n\n'
                    '· 원래 Windows 자동 실행 항목은 그대로 남지만 꺼집니다.\n'
                    '· 로그온 때 시작 관리자가 이 설정(최소화·지연·중복 방지)대로 켭니다.\n'
                    '· 나중에 "시작 관리자에서 빼기"를 누르면 원래대로 돌아갑니다.'):
                return
            self.run_task(lambda: core.adopt(e, new), lambda it: self.statusBar().showMessage(
                f'"{e.name}" 을(를) 시작 관리자가 켜도록 옮기고 설정을 저장했습니다.'), f'"{e.name}" 옮기는 중…',
                keep_key=f'managed:{new.id}')
            return
        item_id = e.managed_id if e.source == 'managed' else e.adopted_by
        if not item_id:
            return
        it = self._find_item(item_id)
        if it is None:
            QMessageBox.warning(self, APP_TITLE, '이 항목을 설정 파일에서 찾지 못했습니다. 새로 고침해 주세요.')
            return
        dlg = ItemDialog(self, it)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new = dlg.item()
            self.run_task(lambda: core.update_item(new), None, '저장 중…', keep_key=e.key)

    def on_new(self):
        dlg = ItemDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new = dlg.item()
            self.run_task(lambda: core.update_item(new), None, '추가 중…', keep_key=f'managed:{new.id}')

    def on_move(self, offset: int):
        e = self.selected_entry()
        if not e or e.source != 'managed':
            return
        self.run_task(lambda: core.move_item(e.managed_id, offset), None, '순서 바꾸는 중…', keep_key=e.key)

    def on_run_now(self):
        e = self.selected_entry()
        if not e or e.source != 'managed':
            return
        it = self._find_item(e.managed_id)
        if it is None:
            QMessageBox.warning(self, APP_TITLE, '이 항목을 설정 파일에서 찾지 못했습니다. 새로 고침해 주세요.')
            return
        extra = ' (Steam 은 로그인될 때까지 몇 분 걸릴 수 있습니다)' if it.type == 'steam' else ''

        def after(result):
            if str(result).startswith('실패'):
                QMessageBox.warning(self, APP_TITLE, f'"{it.name}": {result}')
            else:
                self._notify(f'"{it.name}": {result}')
            self.statusBar().showMessage(f'"{it.name}": {result}')

        self.run_task(lambda: core.launch_item(it), after, f'"{it.name}" 실행 중…{extra}', keep_key=e.key)

    def on_release(self):
        e = self.selected_entry()
        if not e or e.source != 'managed':
            return
        it = self._find_item(e.managed_id)
        text = f'"{e.name}" 을(를) 시작 관리자에서 뺄까요?\n\n· 시작 관리자가 켜는 목록에서 이 항목이 빠집니다.\n'
        if it and it.origin:
            text += '· 레지스트리/시작프로그램 폴더에서 옮겨 온 항목이라 원래 항목이 다시 켜집니다.\n'
        else:
            text += '· 직접 만든 항목이라 되돌릴 원래 항목은 없습니다 (로그온 때 더는 켜지지 않음).\n'
        if not self._confirm(text):
            return
        self.run_task(lambda: core.release(e.managed_id),
                      lambda _: self.statusBar().showMessage(f'"{e.name}" 을(를) 시작 관리자에서 뺐습니다.'),
                      f'"{e.name}" 빼는 중…')

    def on_delete(self):
        e = self.selected_entry()
        if not e or not self.btn_delete.isEnabled():
            return
        what = {
            'managed': '시작 관리자가 켜는 목록에서 지웁니다.',
            'task': '작업 스케줄러에서 작업을 삭제합니다.',
            'folder_user': '바로 가기 파일을 휴지통으로 보냅니다.',
            'folder_common': '바로 가기 파일을 휴지통으로 보냅니다.',
        }.get(e.source, '레지스트리 Run 값을 삭제합니다.')
        text = (f'"{e.name}" 을(를) 목록에서 아예 삭제할까요?\n\n· {what}\n')
        if e.source == 'managed':
            it = self._find_item(e.managed_id)
            if it and it.origin:
                text += f'· 옮겨 오기 전 원래 항목({it.origin.get("name")})도 같이 지웁니다.\n'
        if e.needs_admin:
            text += '· 관리자 권한 확인 창이 뜰 수 있습니다.\n'
        text += f'· 지우기 전 내용은 백업해 둡니다:\n  {core.BACKUP_DIR}\n\n끄기만 하려면 "아니요" 후 "끄기"를 누르세요.'
        if not self._confirm(text):
            return
        self.run_task(lambda: core.delete_entry(e),
                      lambda b: self.statusBar().showMessage(f'"{e.name}" 을(를) 삭제했습니다. 백업: {b}'),
                      f'"{e.name}" 삭제 중…')

    def on_double_click(self, item, _col):
        e = item.data(0, ROLE_ENTRY)
        if e and self.btn_settings.isEnabled():
            self.on_settings()

    def on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None or item.data(0, ROLE_ENTRY) is None:
            return
        self.tree.setCurrentItem(item)
        self.update_buttons()
        menu = QMenu(self)
        actions = [
            (self.btn_toggle, self.on_toggle),
            (self.btn_adopt, self.on_adopt),
            (self.btn_settings, self.on_settings),
            (self.btn_up, lambda: self.on_move(-1)),
            (self.btn_down, lambda: self.on_move(1)),
            (self.btn_run, self.on_run_now),
            (self.btn_release, self.on_release),
            (self.btn_delete, self.on_delete),
        ]
        for btn, fn in actions:
            if btn.isEnabled():
                act = QAction(btn.text(), menu)
                act.triggered.connect(fn)
                menu.addAction(act)
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()


# ───────────────────────── 앱 설정 ─────────────────────────

def apply_style(app: QApplication):
    app.setStyle('Fusion')
    app.setFont(QFont('Malgun Gothic', 9))
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor('#f5f6f8'))
    pal.setColor(QPalette.ColorRole.WindowText, QColor('#222222'))
    pal.setColor(QPalette.ColorRole.Base, QColor('#ffffff'))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor('#f8f9fb'))
    pal.setColor(QPalette.ColorRole.Text, QColor('#222222'))
    pal.setColor(QPalette.ColorRole.Button, QColor('#ffffff'))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor('#222222'))
    pal.setColor(QPalette.ColorRole.Highlight, QColor('#2f6fd6'))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor('#ffffff'))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor('#ffffe8'))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor('#222222'))
    app.setPalette(pal)
    app.setStyleSheet("""
        QPushButton { padding: 5px 12px; border: 1px solid #c5cad3; border-radius: 5px; background: #ffffff; }
        QPushButton:hover { background: #eef3fb; }
        QPushButton:disabled { color: #a8a8a8; background: #f1f2f4; }
        QTreeWidget { border: 1px solid #d3d7de; border-radius: 5px; }
        QTreeWidget::item { padding: 3px 2px; }
        QHeaderView::section { background: #eceff4; padding: 5px; border: none;
                               border-right: 1px solid #d3d7de; font-weight: 600; }
        QLineEdit, QSpinBox, QComboBox, QPlainTextEdit { border: 1px solid #c5cad3; border-radius: 4px; padding: 3px; }
        QTabWidget::pane { border: 1px solid #d3d7de; border-radius: 5px; background: #ffffff; }
    """)


def selftest(win: MainWindow, app: QApplication):
    def report():
        counts: dict[str, int] = {}
        for e in win._entries:
            counts[e.source_label] = counts.get(e.source_label, 0) + 1
        print('SELFTEST OK 총', len(win._entries), '개', flush=True)
        for i in range(win.tree.topLevelItemCount()):
            h = win.tree.topLevelItem(i)
            print(f'  {h.text(0)} -> 행 {h.childCount()}', flush=True)
        print('  런처 라벨:', win.launcher_label.text(), flush=True)
        print('  로그 글자 수:', len(win.log_view.toPlainText()), flush=True)
        # 대화상자도 한 번 만들어 본다 (표시 안 함)
        d = ItemDialog(win)
        d._type_changed()
        QTimer.singleShot(0, app.quit)

    win.refreshed.connect(report)
    QTimer.singleShot(60000, lambda: (print('SELFTEST TIMEOUT', flush=True), app.exit(1)))


def main() -> int:
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_TITLE)
    is_test = '--selftest' in sys.argv

    server = None
    if not is_test:
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(300):
            sock.write(b'show')
            sock.flush()
            sock.waitForBytesWritten(300)
            sock.disconnectFromServer()
            return 0
        QLocalServer.removeServer(SERVER_NAME)
        server = QLocalServer()
        server.listen(SERVER_NAME)

    apply_style(app)
    win = MainWindow()
    if server is not None:
        def on_conn():
            c = server.nextPendingConnection()
            if c:
                c.readyRead.connect(c.readAll)
                c.disconnectFromServer()
            win.bring_to_front()
        server.newConnection.connect(on_conn)
    if is_test:
        selftest(win, app)
    if '--tray' not in sys.argv or win.tray is None:
        win.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
