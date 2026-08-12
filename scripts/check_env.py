#!/usr/bin/env python3
"""Print safe environment diagnostics."""

from __future__ import annotations

import os


def main() -> None:
    value = os.environ.get("FINAM_TOKEN")
    print("FINAM_TOKEN visible:", bool(value), "len:", len(value) if value else 0)


if __name__ == "__main__":
    main()
