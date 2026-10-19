"""Build app/static/icons.svg from the Lucide SVGs in app/static/iconsrc.

Lucide icons are ISC-licensed 24x24 stroke drawings. Collecting them into one
sprite means the UI references a single file:

    <svg class="icon"><use href="/icons.svg#i-search"/></svg>

Run after adding or changing an icon: `python tools/build_icons.py`.
"""

from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(HERE), "app", "static")
SOURCE = os.path.join(STATIC, "iconsrc")
TARGET = os.path.join(STATIC, "icons.svg")


def inner(svg: str) -> str:
    """The drawing without its outer <svg> element."""
    body = re.sub(r"^.*?<svg[^>]*>", "", svg, flags=re.S)
    body = re.sub(r"</svg>.*$", "", body, flags=re.S)
    return " ".join(body.split())


def main() -> int:
    names = sorted(name[:-4] for name in os.listdir(SOURCE) if name.endswith(".svg"))
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" '
        'style="position:absolute;width:0;height:0;overflow:hidden">'
    ]
    for name in names:
        with open(os.path.join(SOURCE, name + ".svg"), encoding="utf-8") as handle:
            parts.append(
                f'<symbol id="i-{name}" viewBox="0 0 24 24">{inner(handle.read())}</symbol>'
            )
    parts.append("</svg>")
    with open(TARGET, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts) + "\n")
    print(f"wrote {TARGET} with {len(names)} icons")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
