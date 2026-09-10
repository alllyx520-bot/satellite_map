"""Build a source-only candidate with an integrity manifest; does not deploy."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile


ROOT_FILES = {"manage.py", "requirements.txt", "requirements-prod.txt", "LICENSE"}
SOURCE_TREES = {
    "map_api": {".py"}, "satellite_map": {".py"},
    "templates": {".html"},
    "static": {".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2", ".ttf"},
    "deploy": {".service", ".conf"},
}


def source_files(root):
    """Only visit runtime source trees; never traverse caches, credentials or data."""
    for name in sorted(ROOT_FILES):
        path = root / name
        if path.is_file() and not path.is_symlink():
            yield path
    for folder, suffixes in sorted(SOURCE_TREES.items()):
        base = root / folder
        if base.is_symlink():
            raise ValueError("source tree must not be a symlink")
        for current, directories, files in os.walk(base, followlinks=False):
            directories[:] = sorted(name for name in directories if not name.startswith(".")
                                    and name not in {"__pycache__", "node_modules"}
                                    and not (Path(current) / name).is_symlink())
            for name in sorted(files):
                path = Path(current) / name
                if name.startswith(".") or path.is_symlink() or path.suffix.lower() not in suffixes:
                    continue
                yield path


def package(root, output):
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError("candidate already exists; use a new output name")
    output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".partial", delete=False) as raw:
            staged = Path(raw.name)
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as archive:
                    for path in source_files(root):
                        data = path.read_bytes()
                        name = path.relative_to(root).as_posix()
                        info = tarfile.TarInfo(name)
                        info.size, info.mode = len(data), 0o644
                        archive.addfile(info, io.BytesIO(data))
                        records.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                    manifest = json.dumps({"schema_version": 1, "artifact_type": "source_candidate",
                                           "release_acceptance": "not_evaluated", "files": records}, sort_keys=True).encode("utf-8")
                    info = tarfile.TarInfo("candidate-manifest.json")
                    info.size, info.mode = len(manifest), 0o644
                    archive.addfile(info, io.BytesIO(manifest))
        with tarfile.open(staged, "r:gz") as archive:
            manifest = json.load(archive.extractfile("candidate-manifest.json"))
            if set(archive.getnames()) != {item["path"] for item in records} | {"candidate-manifest.json"}:
                raise ValueError("archive manifest does not match its members")
            for record in manifest["files"]:
                content = archive.extractfile(record["path"]).read()
                if len(content) != record["bytes"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                    raise ValueError("candidate content failed integrity verification")
        # An exclusive destination prevents replacing another candidate.
        with output.open("xb") as destination, staged.open("rb") as source:
            import shutil
            shutil.copyfileobj(source, destination)
        return {"path": str(output), "bytes": output.stat().st_size, "files": len(records),
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "release_acceptance": "not_evaluated"}
    finally:
        if staged and staged.exists():
            staged.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(package(Path(__file__).resolve().parents[1], args.output), ensure_ascii=False, indent=2))
