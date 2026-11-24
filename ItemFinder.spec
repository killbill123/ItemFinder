# PyInstaller build recipe:  pyinstaller --noconfirm ItemFinder.spec
# Produces dist/ItemFinder/ItemFinder.exe (+ supporting files).
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# torchvision's compiled ops (_C / _C_stable) must be bundled explicitly -
# newer versions renamed them and PyInstaller's default rule misses them.
import glob, importlib.util, os

_tv = os.path.dirname(importlib.util.find_spec("torchvision").origin)
binaries = collect_dynamic_libs("torchvision") + [
    (f, "torchvision")
    for ext in ("*.pyd", "*.so", "*.dll")
    for f in glob.glob(os.path.join(_tv, ext))
]
datas = collect_data_files("open_clip") + [("assets/itemfinder.ico", "assets")]
hiddenimports = (
    collect_submodules("open_clip", filter=lambda m: "test" not in m)
    + ["send2trash", "pillow_heif", "updater", "version", "safetensors", "safetensors.torch"]
)

excludes = [
    "tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "pandas",
    "torch.utils.tensorboard", "tensorboard", "caffe2",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml",
    "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtBluetooth", "PySide6.QtLocation", "PySide6.QtPositioning",
]

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ItemFinder",
    console=False,
    icon="assets/itemfinder.ico",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ItemFinder", upx=False)
