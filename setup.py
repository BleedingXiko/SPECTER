from pathlib import Path
import re

from setuptools import find_packages, setup


ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
INIT = ROOT / "specter" / "__init__.py"


def read_version():
    content = INIT.read_text(encoding="utf-8")
    match = re.search(r"^__version__ = ['\"]([^'\"]+)['\"]$", content, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not find __version__ in specter/__init__.py")
    return match.group(1)


setup(
    name="specter-runtime",
    version=read_version(),
    description="Lifecycle-first backend framework for Flask and gevent.",
    long_description=README.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="BleedingXiko",
    license="Apache-2.0",
    python_requires=">=3.9",
    packages=find_packages(include=["specter", "specter.*"]),
    include_package_data=False,
    install_requires=[
        "Flask>=2.0",
        "gevent>=22.0",
        "Flask-SocketIO>=5.0",
    ],
)
