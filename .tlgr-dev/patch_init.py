import re

PATH = "tlgr/models/__init__.py"
src = open(PATH).read()

import importlib.util
import sys

sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location("_admin", "tlgr/models/admin.py")
# Import lazily via the package instead — simpler and safe.
from tlgr.models import admin  # noqa: E402

names = sorted(admin.__all__)
block = "from tlgr.models.admin import (\n" + "".join(f"    {n},\n" for n in names) + ")\n"

anchor = "from tlgr.models.base import"
src = src.replace(anchor, block + anchor, 1)

match = re.search(r"__all__ = \[\n(.*?)\n\]\n", src, re.S)
existing = re.findall(r'"([^"]+)"', match.group(1))
merged = sorted(set(existing) | set(names), key=lambda s: (s.startswith("_"), s))
new_all = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in merged) + "]\n"
src = src[: match.start()] + new_all + src[match.end() :]
open(PATH, "w").write(src)
print("added", len(names))
