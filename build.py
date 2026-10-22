#!/usr/bin/env python3
"""Build the native app with macOS tools; no Xcode license or extra package needed."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

SOURCE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=SOURCE / 'dist')
parser.add_argument('--python', type=Path, default=Path(sys.executable), help='App 使用的 Python 3.11+ 解释器')
parser.add_argument('--legacy-state', type=Path, help='可选：接管旧中继的状态目录')
args = parser.parse_args()
output = args.output.expanduser().resolve()
runtime = args.python.expanduser().absolute()
if not runtime.is_file() or not os.access(runtime, os.X_OK):
    parser.error('--python 必须指向可执行的 Python 解释器')
subprocess.run([str(runtime), '-c', 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11+"'], check=True)
output.mkdir(parents=True, exist_ok=True)
name = '云连接助手 1.0.app'
destination = output / name
archive = output / '云连接助手 1.0.zip'
if destination.exists() or archive.exists():
    parser.error('输出文件已存在，请使用新的 --output 目录，避免覆盖已有 App')

# Signing outside Documents avoids File Provider re-adding Finder metadata mid-build.
with tempfile.TemporaryDirectory(prefix='cloudlink-build-', dir='/private/tmp') as directory:
    app = Path(directory) / name
    script = (SOURCE / 'main.applescript').read_text()
    replacements = {
        '__CLOUDLINK_PYTHON__': str(runtime),
        '__CLOUDLINK_LEGACY_STATE__': str(args.legacy_state.expanduser().resolve()) if args.legacy_state else '',
    }
    for marker, value in replacements.items():
        script = script.replace(marker, value.replace('\\', '\\\\').replace('"', '\\"'))
    script_path = Path(directory) / 'main.applescript'
    script_path.write_text(script)
    subprocess.run(['/usr/bin/osacompile', '-s', '-o', str(app), str(script_path)], check=True)
    resources = app / 'Contents/Resources'
    for resource in ['relay.py', 'antigravity_helper.py']:
        shutil.copyfile(SOURCE / resource, resources / resource)
    shutil.copyfile('/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericNetworkIcon.icns', resources / 'appicon.icns')
    info_path = app / 'Contents/Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    info.update(CFBundleIdentifier='local.hive.cloudlinkassistant', CFBundleDisplayName='云连接助手',
                CFBundleName='云连接助手', CFBundleShortVersionString='1.0.0', CFBundleVersion='2',
                CFBundleIconFile='appicon.icns', NSHighResolutionCapable=True, OSAAppletStayOpen=True)
    info_path.write_bytes(plistlib.dumps(info))
    for attr in ['com.apple.FinderInfo', 'com.apple.ResourceFork']:
        subprocess.run(['/usr/bin/xattr', '-dr', attr, str(app)], capture_output=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    shutil.copytree(app, destination)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(app.rglob('*')):
            if path.is_file():
                bundle.write(path, str(Path(name) / path.relative_to(app)))
    print(destination)
    print(archive)
