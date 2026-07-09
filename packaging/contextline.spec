# PyInstaller spec for Contextline.app (macOS, Apple Silicon).
#
# onedir (not onefile): a folder bundle is faster to start and far more reliable with a big,
# dynamically-imported dependency tree like google-adk than unpacking a onefile at every launch.
#
# The heavy lifting is collect_all over the packages that PyInstaller's static analysis can't
# fully follow (google.adk / google.genai pull data files and lazy submodules; uvicorn imports
# its protocol/loop backends by string). Run from the repo root:
#     pyinstaller packaging/contextline.spec --noconfirm
#
# The build env must have the project installed (`pip install -e '.[capture]'`) so `contextline`
# and the mss/pyobjc capture deps are importable and get bundled.

from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

datas, binaries, hiddenimports = [], [], []


def _add_all(pkg):
    try:
        d, b, h = collect_all(pkg)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(h)
    except Exception as exc:  # a missing optional package shouldn't abort the whole build
        print(f"[spec] collect_all({pkg!r}) skipped: {exc}")


# Packages whose data/lazy imports PyInstaller misses on its own.
for pkg in (
    "google.adk",
    "google.genai",
    "google.auth",
    "google.api_core",
    "opentelemetry",
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "anyio",
    "mss",          # screenshots (capture child)
    "objc",         # pyobjc core
    "Quartz",       # CGWindowList (window title / z-order)
    "Cocoa",        # NSWorkspace
    "tzdata",       # zoneinfo data, if present
):
    _add_all(pkg)

# Our own package: bundle the code and the chat UI static file.
hiddenimports += collect_submodules("contextline")
datas += collect_data_files("contextline", includes=["api/static/*"])

# uvicorn resolves these by string at runtime.
hiddenimports += collect_submodules("uvicorn")

a = Analysis(
    ["entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "PySide6", "PyQt6", "IPython", "notebook"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Contextline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no terminal window; it's a GUI-launched local server
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Contextline",
)

app = BUNDLE(
    coll,
    name="Contextline.app",
    icon=None,
    bundle_identifier="com.contextline.app",
    info_plist={
        "CFBundleName": "Contextline",
        "CFBundleDisplayName": "Contextline",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        # We open the user's default browser at the local server, so no in-app WebView is needed.
        # Screen Recording is requested at runtime by the capture child (TCC prompt, attributed to
        # this bundle id) — no Info.plist usage string exists for Screen Recording on macOS.
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Contextline — runs locally on your Mac.",
    },
)
