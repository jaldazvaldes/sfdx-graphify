"""Build the standalone Salesforce wheel from the fork's shared source."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--no-isolation", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.out_dir.resolve()
    with TemporaryDirectory(prefix="sfdx-wheel-") as temp:
        stage = Path(temp)
        shutil.copyfile(root / "packaging" / "salesforce" / "pyproject.toml", stage / "pyproject.toml")
        for name in ("LICENSE", "LICENSE-MIT", "NOTICE"):
            shutil.copyfile(root / name, stage / name)
        for package in ("graphify", "graphify/extractors", "graphify/exporters", "graphify/salesforce"):
            package_dir = stage / package
            package_dir.mkdir(parents=True, exist_ok=True)
            for module in (root / package).glob("*.py"):
                shutil.copyfile(module, package_dir / module.name)
        source = root / "graphify" / "salesforce"
        target = stage / "graphify" / "salesforce"
        shutil.copytree(source / "viewer_static", target / "viewer_static")
        shutil.copytree(source / "skills" / "sfdx-graphify", target / "skills" / "sfdx-graphify")
        command = [sys.executable, "-m", "build", "--wheel", "--outdir", str(destination)]
        if args.no_isolation:
            command.append("--no-isolation")
        subprocess.run(command, cwd=stage, check=True)


if __name__ == "__main__":
    main()
