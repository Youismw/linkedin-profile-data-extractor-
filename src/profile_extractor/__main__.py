"""Direct module execution entry point for `python -m profile_extractor`.

Communicates directly with `profile_extractor.cli.main()` to run the command-line interface.
"""

import sys

from profile_extractor.cli import main

if __name__ == "__main__":
    # Hand off execution directly to CLI main dispatcher
    sys.exit(main())
