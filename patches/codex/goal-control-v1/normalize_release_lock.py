"""Derive the pinned release build lock without changing third-party resolution.

Source preparation only. Does not fetch, execute Cargo, or modify the input.
Usage: python3 normalize_release_lock.py ORIGINAL OUTPUT
"""

import hashlib
from pathlib import Path
import re
import sys
import tomllib

ORIGINAL = "17cdd862471774ba7aef836aaf0a0a3ed76bd23e503b1fc7278c89d1f2719d03"
DERIVED = "cae541406f7ec43a418e126be2bcd5de00b243e7bbf6382570028727d99c1069"


def derive(raw: bytes) -> bytes:
    if hashlib.sha256(raw).hexdigest() != ORIGINAL:
        raise ValueError("not the pinned official release source lock")
    before = tomllib.loads(raw.decode())
    local = {p["name"] for p in before["package"] if "source" not in p}
    if len(local) != 160:
        raise ValueError("unexpected local package set")
    blocks = raw.decode().split("[[package]]")
    for index in range(1, len(blocks)):
        block = blocks[index]
        package = tomllib.loads("[[package]]" + block)["package"][0]
        if "source" not in package:
            if package["version"] != "0.0.0":
                raise ValueError("unexpected local package version")
            block = block.replace('version = "0.0.0"', 'version = "0.163.0"', 1)
        for name in local:
            block = block.replace('"' + name + ' 0.0.0"', '"' + name + ' 0.163.0"')
        blocks[index] = block
    result = "[[package]]".join(blocks).encode()
    after = tomllib.loads(result.decode())
    # Normalize only explicit local dependency references for graph comparison.
    def edges(package):
        return [re.sub(r"^(.*) 0\.163\.0$", lambda m: m[1] + " 0.0.0"
                       if m[1] in local else m[0], d)
                for d in package.get("dependencies", [])]
    if before.keys() != after.keys() or len(before["package"]) != len(after["package"]):
        raise ValueError("lock structure changed")
    for old, new in zip(before["package"], after["package"]):
        expected = dict(old)
        observed = dict(new)
        if "source" not in old:
            expected["version"] = "0.163.0"
        if "dependencies" in observed:
            observed["dependencies"] = edges(observed)
        if expected != observed:
            raise ValueError("unexpected source/checksum/version/edge change")
    for key in before.keys() - {"package"}:
        if before[key] != after[key]:
            raise ValueError("non-package lock metadata changed")
    if hashlib.sha256(result).hexdigest() != DERIVED:
        raise ValueError("derived lock does not match qualified build input")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: normalize_release_lock.py ORIGINAL OUTPUT")
    source, output = map(Path, sys.argv[1:])
    result = derive(source.read_bytes())
    # Exclusive creation preserves original and any previous qualification input.
    with output.open("xb") as stream:
        stream.write(result)
