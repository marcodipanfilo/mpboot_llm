"""
Compatibility wrapper for workspace cleanup.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runners.common import clear_workspace


def main() -> None:
    clear_workspace(confirm="--confirm" in sys.argv)


if __name__ == "__main__":
    main()
