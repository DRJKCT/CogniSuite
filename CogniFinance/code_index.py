"""
code_index.py — deterministic function/call-site index for a Python project.

Why: lets a local LLM (or you) look up "where is X defined" / "who calls X"
without reading through every module. Build once, query many times; rebuild
whenever the codebase changes meaningfully (wire this into a pre-commit hook
or just rerun `build` after a session of edits).

Usage:
    python code_index.py build <project_root> -o index.json
    python code_index.py lookup <name> -i index.json

This is intentionally a skeleton: name-based matching, not full symbol
resolution. Two classes with a method of the same name will collide in the
index. That's an acceptable first pass — see "Known limitations" at the
bottom for the upgrade path (jedi) if that starts to matter for you.
"""

import argparse
import ast
import json
from pathlib import Path
from collections import defaultdict


def iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        # adjust/extend this skip list for your project
        if any(part in {".venv", "venv", "__pycache__", ".git", "build"} for part in path.parts):
            continue
        yield path


class IndexVisitor(ast.NodeVisitor):
    """Walks one file's AST, recording definitions and call sites."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.definitions = []   # [{name, kind, file, line}]
        self.calls = []         # [{name, file, line}]
        self._class_stack = []

    def visit_ClassDef(self, node: ast.ClassDef):
        self.definitions.append({
            "name": node.name,
            "kind": "class",
            "file": self.filepath,
            "line": node.lineno,
        })
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _visit_func(self, node):
        qualname = node.name
        if self._class_stack:
            # record both qualified and bare name so lookups work either way
            qualname = f"{self._class_stack[-1]}.{node.name}"
        self.definitions.append({
            "name": node.name,
            "qualname": qualname,
            "kind": "method" if self._class_stack else "function",
            "file": self.filepath,
            "line": node.lineno,
        })
        self.generic_visit(node)

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr  # e.g. obj.method(...) -> "method"
        if name:
            self.calls.append({
                "name": name,
                "file": self.filepath,
                "line": node.lineno,
            })
        self.generic_visit(node)


def build_index(root: Path) -> dict:
    definitions = defaultdict(list)
    calls = defaultdict(list)

    for path in iter_python_files(root):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as e:
            print(f"  skipped {path}: {e}")
            continue

        rel = str(path.relative_to(root))
        visitor = IndexVisitor(rel)
        visitor.visit(tree)

        for d in visitor.definitions:
            definitions[d["name"]].append(d)
        for c in visitor.calls:
            calls[c["name"]].append(c)

    # merge into one lookup structure: name -> {defined_in: [...], called_from: [...]}
    all_names = set(definitions) | set(calls)
    index = {
        name: {
            "defined_in": definitions.get(name, []),
            "called_from": calls.get(name, []),
        }
        for name in sorted(all_names)
    }
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="scan project and write index.json")
    p_build.add_argument("root", type=Path)
    p_build.add_argument("-o", "--output", type=Path, default=Path("index.json"))

    p_lookup = sub.add_parser("lookup", help="look up a function/class/method name")
    p_lookup.add_argument("name")
    p_lookup.add_argument("-i", "--index", type=Path, default=Path("index.json"))

    args = parser.parse_args()

    if args.command == "build":
        print(f"Indexing {args.root} ...")
        index = build_index(args.root)
        args.output.write_text(json.dumps(index, indent=2))
        print(f"Wrote {len(index)} names to {args.output}")

    elif args.command == "lookup":
        index = json.loads(args.index.read_text())
        entry = index.get(args.name)
        if not entry:
            print(f"'{args.name}' not found in index.")
            return
        print(f"\n=== {args.name} ===")
        print("Defined in:")
        for d in entry["defined_in"]:
            print(f"  {d['file']}:{d['line']}  ({d['kind']})")
        print(f"\nCalled from ({len(entry['called_from'])} site(s)):")
        for c in entry["called_from"]:
            print(f"  {c['file']}:{c['line']}")


if __name__ == "__main__":
    main()

