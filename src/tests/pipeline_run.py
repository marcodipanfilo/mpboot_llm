"""
Compatibility wrapper for the single-dataset runner.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runners.create_mapping_single_dataset import main


if __name__ == "__main__":
    main()
