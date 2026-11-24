"""배포본 만들기: python build.py → build_out\dist\StartupManager\ 와 build_out\StartupManager.zip"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / 'build_out'
WORK.mkdir(parents=True, exist_ok=True)

# 아이콘(.ico) 만들기 - GUI 트레이 아이콘과 같은 그림
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtGui import QPixmap, QPainter, QColor  # noqa: E402
from PyQt6.QtCore import Qt, QPoint  # noqa: E402

app = QApplication(sys.argv)
pm = QPixmap(256, 256)
pm.fill(Qt.GlobalColor.transparent)
p = QPainter(pm)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setBrush(QColor('#2f6fd6'))
p.setPen(Qt.PenStyle.NoPen)
p.drawRoundedRect(16, 16, 224, 224, 56, 56)
p.setBrush(QColor('#ffffff'))
p.drawPolygon(*[QPoint(x * 4, y * 4) for x, y in ((24, 17), (24, 47), (48, 32))])
p.end()
png = WORK / 'icon.png'
pm.save(str(png))
from PIL import Image  # noqa: E402
ico = WORK / 'icon.ico'
Image.open(png).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])

cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
       '--name', 'StartupManager', '--icon', str(ico),
       '--distpath', str(WORK / 'dist'), '--workpath', str(WORK / 'build'), '--specpath', str(WORK),
       '--hidden-import', 'win32com.shell.shell', '--hidden-import', 'win32timezone',
       '--exclude-module', 'torch', '--exclude-module', 'numpy', '--exclude-module', 'tkinter',
       '--exclude-module', 'matplotlib', '--exclude-module', 'pandas',
       str(HERE / 'app.py')]
print(' '.join(cmd), flush=True)
subprocess.run(cmd, check=True, cwd=HERE)

out = WORK / 'dist' / 'StartupManager'
shutil.copy(HERE / '사용법.txt', out / '사용법.txt')
zip_base = WORK / 'StartupManager'
shutil.make_archive(str(zip_base), 'zip', root_dir=out.parent, base_dir=out.name)
print('완료:', zip_base.with_suffix('.zip'), flush=True)
