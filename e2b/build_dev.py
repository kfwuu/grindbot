"""Build the GrindBot E2B sandbox template.

Run once from the e2b/ directory:
    python build_dev.py

Prints the template name on success. Add it to ~/.env as:
    E2B_TEMPLATE_ID=grindbot-gemini
"""
import os
import sys
from pathlib import Path

# Load E2B_API_KEY from ~/.env if not already in environment
if not os.environ.get("E2B_API_KEY"):
    env_file = Path.home() / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("E2B_API_KEY="):
                os.environ["E2B_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

if not os.environ.get("E2B_API_KEY"):
    print("ERROR: E2B_API_KEY not set. Add it to ~/.env", file=sys.stderr)
    sys.exit(1)

from e2b import Template, default_build_logger
from template import template

print("Building GrindBot sandbox template...")
print("This takes a few minutes the first time (installing Node + Gemini CLI).")
print()

build_info = Template.build(
    template,
    "grindbot-gemini",
    cpu_count=2,
    memory_mb=2048,
    on_build_logs=default_build_logger(),
)

print()
print(f"✓ Template built: grindbot-gemini")
print(f"  Template ID: {build_info.template_id}")
print()
print("Add to ~/.env:")
print(f"  E2B_TEMPLATE_ID=grindbot-gemini")
