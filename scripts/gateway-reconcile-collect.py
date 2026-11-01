#!/usr/bin/env python3
import json, pathlib, subprocess, sys
conf_dir, node = sys.argv[1], sys.argv[2]
if conf_dir:
    out = {}
    for f in sorted(pathlib.Path(conf_dir).glob("*.conf")):
        for line in f.read_text().splitlines():
            if line.startswith("description: "):
                # pmxcfs keeps the description as one escaped single line
                out[f.stem] = line[len("description: "):].replace("\\n", "\n")
    print(json.dumps(out))
    sys.exit(0)
ids = subprocess.run(
    ["pvesh", "get", f"/nodes/{node}/lxc", "--output-format", "json"],
    capture_output=True, text=True, check=True)
out = {}
for guest in json.loads(ids.stdout):
    vid = str(guest["vmid"])
    cfg = subprocess.run(
        ["pvesh", "get", f"/nodes/{node}/lxc/{vid}/config", "--output-format", "json"],
        capture_output=True, text=True, check=True)
    out[vid] = json.loads(cfg.stdout).get("description", "")
print(json.dumps(out))
