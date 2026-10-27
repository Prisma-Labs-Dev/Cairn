from __future__ import annotations

import os
from pathlib import Path
import stat

from cairn.server.artifact_models import validate_artifact_path


def read_relative_file(root: Path, path: str, max_bytes: int) -> bytes:
    """Open each component without following symlinks, including during a rename race."""
    validate_artifact_path(path)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("artifact must be a regular file")
            if info.st_size > max_bytes:
                raise ValueError("artifact exceeds the 10 MiB limit")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                content = source.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError("artifact exceeds the 10 MiB limit")
            return content
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


# Use the container's configured working directory. Directory-descriptor traversal rejects
# symlinked parents, which a plain Docker get_archive(path) cannot safely guarantee.
CONTAINER_READ_SCRIPT = """\
import os, stat, sys
path, limit = sys.argv[1], int(sys.argv[2])
directory = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
try:
    parts = path.split('/')
    for part in parts[:-1]:
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = child
    descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('artifact must be a regular file')
        if info.st_size > limit:
            raise ValueError('artifact exceeds the 10 MiB limit')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            content = source.read(limit + 1)
        if len(content) > limit:
            raise ValueError('artifact exceeds the 10 MiB limit')
        sys.stdout.buffer.write(content)
    finally:
        os.close(descriptor)
finally:
    os.close(directory)
"""
