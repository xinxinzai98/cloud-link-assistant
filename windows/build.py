"""Build the Windows x64 workers archive with the official embedded Python."""

import hashlib
import io
from pathlib import Path
import tempfile
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "v1.2.1-windows.1"
PYTHON_URL = "https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip"
PYTHON_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
BUNDLE = "cloud-link-assistant-windows"


def main():
    with urlopen(PYTHON_URL, timeout=60) as response:
        runtime = response.read(20 * 1024 * 1024)
    if hashlib.sha256(runtime).hexdigest() != PYTHON_SHA256:
        raise ValueError("The Python archive does not match the checksum from python.org")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    asset = dist / f"cloud-link-assistant-{VERSION}-x64.zip"
    files = [ROOT / name for name in ("relay.py", "https_compat.py", "windows_bridge.py",
                                     "THIRD_PARTY_NOTICES.md")]
    files.extend(ROOT / "windows" / name for name in ("README.md", "Start-Model.cmd", "Start-Cloud.cmd"))
    with tempfile.TemporaryDirectory(dir=dist) as directory:
        temporary = Path(directory) / asset.name
        with ZipFile(io.BytesIO(runtime)) as source, ZipFile(temporary, "w", ZIP_DEFLATED) as output:
            for entry in source.infolist():
                data = source.read(entry)
                if entry.filename == "python314._pth":
                    data = data.rstrip(b"\r\n") + b"\r\n..\r\n"
                output.writestr(f"{BUNDLE}/python/{entry.filename}", data)
            for path in files:
                output.write(path, f"{BUNDLE}/{path.name}")
        temporary.replace(asset)
    checksum = hashlib.sha256(asset.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{checksum}  {asset.name}\n", encoding="ascii")
    print(asset)


if __name__ == "__main__":
    main()
