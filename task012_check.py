import ast
import os

paths = [
    "app/bootstrap.py",
    "core/provider_registry.py",
    "core/resource_factory.py",
    "providers/gemini_cli/factory.py",
]
paths.extend([os.path.join(dp, f) for dp, _, fs in os.walk("providers/antigravity") for f in fs])
paths.extend([os.path.join(dp, f) for dp, _, fs in os.walk("tests/providers/antigravity") for f in fs])
for p in paths:
    if not os.path.exists(p):
        print(f"--- MISSING {p}")
        continue
    print(f"--- {p}")
    try:
        tree = ast.parse(open(p, encoding="utf-8", errors="replace").read())
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                print(f"{node.lineno}: {'class' if isinstance(node, ast.ClassDef) else 'def'} {node.name}")
    except Exception as e:
        print("AST_ERROR", e)
