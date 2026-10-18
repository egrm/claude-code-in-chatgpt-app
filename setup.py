"""Bootstrap CLI, not a packaging script:

    python setup.py install      (python3 on macOS/Linux)
    python setup.py configure | doctor | remove

Kept free of newer syntax so an old interpreter reaches the version message.
"""
import os
import sys

MINIMUM = (3, 11)


def version_error(version):
    if tuple(version[:2]) < MINIMUM:
        return ('Python %d.%d+ is required (found %d.%d). Pinned dependencies (starlette, rpds-py) and tomllib '
                'need it; run this with a newer python/python3.' % (MINIMUM + tuple(version[:2])))
    return None


if __name__ == '__main__':
    error = version_error(sys.version_info)
    if error:
        sys.stderr.write(error + '\n')
        sys.exit(1)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from installer import main
    sys.exit(main(sys.argv[1:]))
