#!/usr/bin/env python3
"""Prove which envd binary actually serves inside a sandbox built from a template."""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")

from cubesandbox import Config, Sandbox  # noqa: E402

CHECKS = [
    ("sha256 binaries", "sha256sum /usr/bin/envd /usr/local/bin/envd 2>&1"),
    ("usr/bin version", "/usr/bin/envd --version 2>&1; /usr/bin/envd --commit 2>&1"),
    ("usr/local version", "/usr/local/bin/envd --version 2>&1; /usr/local/bin/envd --commit 2>&1"),
    ("envd processes", "ps -eo pid,ppid,args 2>/dev/null | grep [e]nvd"),
    ("listener 49983", "ss -ltnp 2>/dev/null | grep 49983 || cat /proc/net/tcp | awk 'NR>1{print $2}' | head -3"),
    ("exe links", "for p in /proc/[0-9]*/exe; do t=$(readlink $p 2>/dev/null); case \"$t\" in *envd*) echo \"$p -> $t\";; esac; done"),
    ("ENVD_BIN env", "cat /proc/1/environ 2>/dev/null | tr '\\0' '\\n' | grep -E '^ENVD_BIN=' || echo unset"),
    ("health", "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:49983/health 2>&1"),
]


def main() -> int:
    template = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else template
    config = Config(api_url="http://127.0.0.1:3000", template_id=template)
    sandbox = Sandbox.create(timeout=600, config=config)
    out: dict = {"label": label, "template": template, "sandbox_id": sandbox.sandbox_id}
    try:
        for name, cmd in CHECKS:
            try:
                res = sandbox.commands.run(cmd)
                out[name] = {"stdout": res.stdout.strip()[:800], "stderr": res.stderr.strip()[:300],
                             "exit_code": res.exit_code}
            except Exception as exc:  # noqa: BLE001
                out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            sandbox.kill()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
