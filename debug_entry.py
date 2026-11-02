"""Entry point of the debug build: the same app with --debug always on."""
import sys

import aristarkus

if __name__ == '__main__':
    aristarkus.run(['--debug', *sys.argv[1:]])
