from __future__ import annotations

import os
from pathlib import Path
import stat
import re

from cairn.server.artifact_models import validate_artifact_path

MAX_SESSION_CHUNK_BYTES = 256 * 1024
SESSION_PATH_PATTERN = re.compile(r"cairn-sessions/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl\Z")


def validate_session_read(path: str, offset: int, max_bytes: int) -> None:
    if not isinstance(path, str) or not SESSION_PATH_PATTERN.fullmatch(path):
        raise ValueError("session path must be a managed cairn-sessions UUID JSONL file")
    if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
        raise ValueError("session offset must be a non-negative integer")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SESSION_CHUNK_BYTES:
        raise ValueError("session chunks must contain 1 to 262144 bytes")


def read_session_chunk(root: Path, path: str, offset: int, max_bytes: int) -> bytes:
    """Read only a bounded slice of a managed session, never follow a symlink."""
    validate_session_read(path, offset, max_bytes)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        child = os.open("cairn-sessions", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = child
        descriptor = os.open(path.split("/")[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("session must be a regular file")
            if offset > info.st_size:
                raise ValueError("session source shrank below the captured offset")
            return os.pread(descriptor, max_bytes, offset)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


CONTAINER_SESSION_READ_SCRIPT = """\
import os, stat, sys, re
def open_required(*args, **kwargs):
    try:
        return os.open(*args, **kwargs)
    except FileNotFoundError:
        sys.exit(44)
path, offset, limit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
if not re.fullmatch(r'cairn-sessions/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.jsonl', path):
    raise ValueError('session path is not managed')
if not 0 <= offset <= 2**63-1 or not 1 <= limit <= 262144:
    raise ValueError('invalid session slice bounds')
directory = open_required('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    child = open_required('cairn-sessions', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    os.close(directory)
    directory = child
    descriptor = open_required(path.split('/')[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('session must be a regular file')
        if offset > info.st_size:
            raise ValueError('session source shrank below the captured offset')
        sys.stdout.buffer.write(os.pread(descriptor, limit, offset))
    finally:
        os.close(descriptor)
finally:
    os.close(directory)
"""


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
