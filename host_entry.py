"""Absolute-path entrypoint used by the two browsers' Native Messaging launchers."""
from browser_transfer.bridge import native_main

if __name__ == "__main__":
    native_main()
