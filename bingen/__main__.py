import sys

from .app import main

if __name__ == "__main__":
    main(open_browser="--no-browser" not in sys.argv)
