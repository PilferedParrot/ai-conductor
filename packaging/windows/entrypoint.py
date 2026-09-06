"""PyInstaller entry point for the portable Windows executable."""

from pilferedparrot.windows import main


if __name__ == "__main__":
    raise SystemExit(main())
