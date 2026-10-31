#!/usr/bin/env python3
"""Send human-monitor commands to a QEMU monitor unix socket and print the replies.

usage: hmp.py SOCKET COMMAND...
"""
import re
import socket
import sys


def read_reply(sock):
    out = b""
    try:
        while not out.rstrip().endswith(b"(qemu)"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    text = out.decode(errors="replace")
    # Drop the line-editor escape sequences QEMU echoes back.
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def main():
    path, commands = sys.argv[1], sys.argv[2:]
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(5)
    sock.connect(path)
    read_reply(sock)
    for command in commands:
        sock.sendall(command.encode() + b"\n")
        print("> " + command)
        print(read_reply(sock))


if __name__ == "__main__":
    main()
