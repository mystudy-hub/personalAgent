"""Create a standalone Git repository containing the M0 CSV failure baseline."""

import argparse
import shutil
import subprocess
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "csv_import"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New directory for the repository")
    args = parser.parse_args()
    destination = args.destination.absolute()
    destination.mkdir()

    fixture_names = []
    for path in sorted(FIXTURE.iterdir()):
        if path.is_file():
            name = path.name.removesuffix(".fixture")
            shutil.copyfile(path, destination / name)
            fixture_names.append(name)

    def git(*arguments: str) -> None:
        subprocess.run(["git", "-C", str(destination), *arguments], check=True)

    git("init", "--quiet", "--template=", "--initial-branch=main")
    git("add", "--", *fixture_names)
    git(
        "-c", "user.name=PA CSV Fixture",
        "-c", "user.email=pa-fixture@example.invalid",
        "-c", "core.hooksPath=.git/hooks",
        "commit", "--quiet", "--no-gpg-sign", "-m", "Create CSV regression baseline",
    )
    git("rev-parse", "HEAD")


if __name__ == "__main__":
    main()
