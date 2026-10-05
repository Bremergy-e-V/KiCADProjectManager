# PyInstaller entry point. The package's __main__.py uses a relative import,
# which does not work when PyInstaller runs it as a top-level script.
from kicad_project_manager.app import main

main()
