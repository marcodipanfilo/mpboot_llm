"""
Placeholder entrypoint for evaluation workflows.

The mapping-generation refactor now has dedicated runners under src/runners.
Evaluation will be added here when RODI/Ontop integration lands.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    print("Evaluation runner is not implemented yet.")
    print("This module is reserved for the future RODI/Ontop evaluation workflow.")
    sys.exit(1)


if __name__ == "__main__":
    main()
