#!/usr/bin/env python3
"""
Build a .skill file from a skill directory.

A .skill file is a ZIP archive with a different extension. This script
compresses the target directory into {dirname}.skill, preserving the
directory structure inside the archive.

Usage:
    python build_skill.py [path/to/skill-dir]

If no path is given, looks for a single directory in the current working
directory that contains a SKILL.md file.

Output: {skill-dir-name}.skill in the current working directory.
"""

import os
import sys
import zipfile
import argparse


def find_skill_dir(search_root="."):
    """Auto-detect the skill directory by looking for SKILL.md."""
    candidates = []
    for entry in os.listdir(search_root):
        full = os.path.join(search_root, entry)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "SKILL.md")):
            candidates.append(full)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        print(f"Multiple skill directories found: {candidates}", file=sys.stderr)
        print("Specify one explicitly.", file=sys.stderr)
        sys.exit(1)
    return None


def build_skill(skill_dir, output_path=None):
    skill_dir = os.path.normpath(skill_dir)
    if not os.path.isdir(skill_dir):
        print(f"Not a directory: {skill_dir}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
        print(f"No SKILL.md in {skill_dir} — is this a skill directory?", file=sys.stderr)
        sys.exit(1)

    skill_name = os.path.basename(skill_dir)
    if not output_path:
        output_path = os.path.join(".", f"{skill_name}.skill")

    skip = {"__pycache__", ".git", ".DS_Store", "node_modules", ".env"}

    files_added = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in skip]
            for fname in sorted(files):
                if fname.startswith(".") and fname != ".gitkeep":
                    continue
                if fname.endswith((".pyc", ".pyo")):
                    continue
                full_path = os.path.join(root, fname)
                arc_name = os.path.relpath(full_path, os.path.dirname(skill_dir))
                zf.write(full_path, arc_name)
                files_added += 1

    size_kb = os.path.getsize(output_path) / 1024
    print(f"{output_path}  ({files_added} files, {size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Build a .skill file from a skill directory.")
    parser.add_argument("skill_dir", nargs="?", default=None,
                        help="Path to the skill directory (auto-detects if omitted)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output .skill file path (default: {name}.skill)")
    args = parser.parse_args()

    skill_dir = args.skill_dir
    if not skill_dir:
        skill_dir = find_skill_dir()
    if not skill_dir:
        print("No skill directory found. Pass the path explicitly or ensure a directory "
              "with SKILL.md exists in the current directory.", file=sys.stderr)
        sys.exit(1)

    build_skill(skill_dir, args.output)


if __name__ == "__main__":
    main()
