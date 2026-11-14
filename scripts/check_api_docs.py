"""Check the route inventory in the API contract; --update refreshes that section.

This only checks methods and paths. Review the leader guide, request schemas,
permissions, limits and examples separately whenever behavior changes.
Uses only the standard library and never imports the application or secrets.
"""

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- api-routes:start -->"
END = "<!-- api-routes:end -->"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def route_inventory(source):
    tree = ast.parse(source)
    prefix = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "router" for t in node.targets)
            and isinstance(node.value, ast.Call)
        ):
            prefix = next(
                ast.literal_eval(k.value) for k in node.value.keywords if k.arg == "prefix"
            )
    if prefix is None:
        raise ValueError("Cannot find the API router prefix")
    routes = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in node.decorator_list:
            if (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and isinstance(item.func.value, ast.Name)
                and item.func.value.id == "router"
                and item.func.attr in METHODS
            ):
                routes.append((item.func.attr.upper(), prefix + ast.literal_eval(item.args[0])))
    if not routes or len(set(routes)) != len(routes):
        raise ValueError("Empty or duplicate API route inventory")
    return sorted(routes, key=lambda route: (route[1], route[0]))


def inventory_section(routes):
    rows = [START, "| Метод | Путь |", "|---|---|"]
    rows.extend(f"| `{method}` | `{path}` |" for method, path in routes)
    return "\n".join([*rows, END])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    routes = route_inventory((ROOT / "src/cu_clubs/api.py").read_text())
    path = ROOT / "docs/api-integration.md"
    content = path.read_text()
    expected = inventory_section(routes)
    if content.count(START) != 1 or content.count(END) != 1:
        parser.exit(1, "API documentation must contain exactly one route inventory section\n")
    before, remainder = content.split(START)
    current, after = remainder.split(END)
    if args.update:
        path.write_text(before + expected + after)
    elif START + current + END != expected:
        parser.exit(1, "API route docs are stale. Review both guides, then run --update.\n")
    print(f"API documentation inventory matches {len(routes)} operations")


if __name__ == "__main__":
    main()
