#!/usr/bin/env python3
"""Toggle auth.enabled in config.toml for testing. Usage: toggle_auth.py on|off"""
import os
import sys
import re

CONFIG = os.getenv('MNEMOS_CONFIG', '/opt/mnemos/config.toml')

def set_auth_enabled(enabled: bool):
    value = "true" if enabled else "false"
    with open(CONFIG, "r") as f:
        content = f.read()

    # Replace only the enabled line within the [auth] section
    # Pattern: match [auth] block, then the enabled = ... line
    def replace_in_auth(m):
        block = m.group(0)
        block = re.sub(r'^(enabled\s*=\s*)(?:true|false)', lambda x: x.group(1) + value, block, flags=re.MULTILINE)
        return block

    # Match from [auth] to the next [section]
    new_content = re.sub(r'\[auth\].*?(?=\[|\Z)', replace_in_auth, content, flags=re.DOTALL)

    with open(CONFIG, "w") as f:
        f.write(new_content)
    print(f"auth.enabled = {value}")

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("on", "off"):
        print("Usage: toggle_auth.py on|off")
        sys.exit(1)
    set_auth_enabled(sys.argv[1] == "on")
