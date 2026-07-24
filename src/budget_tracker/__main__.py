"""Enable ``python -m budget_tracker``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
