"""Produce a source handout using an explicit allowlist, excluding run data."""

import argparse
from pathlib import Path
import zipfile


def package(output: Path) -> int:
    root = Path(__file__).resolve().parents[1]
    files = [root / name for name in (
        "README.md", "CHALLENGE.md", "SPEC.md", "pyproject.toml", "uv.lock",
        "requirements-client.txt", "requirements-server.txt", "Dockerfile",
        "starter.Dockerfile", "compose.yaml", ".gitignore", ".dockerignore",
    )]
    for directory, pattern in (("aiday", "*.py"), ("starter", "*.py"), ("starter", "*.md"),
                               ("docs", "*.md"), ("examples", "*.json"), ("schemas", "*.json"),
                               ("tests", "*.py"), ("scripts", "*.py")):
        files.extend(sorted((root / directory).glob(pattern)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"Missing or unsafe handout source: {path}")
            archive.write(path, arcname="aiday-starter/" + str(path.relative_to(root)))
    print(f"Packaged {len(set(files))} public source files into {output}")
    return len(set(files))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/aiday-starter.zip"))
    args = parser.parse_args()
    package(args.output)
