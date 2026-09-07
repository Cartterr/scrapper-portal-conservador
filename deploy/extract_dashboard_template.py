"""One-time mechanical extraction of the legacy inline dashboard template."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / "cbrs" / "account_pool_dashboard.py"
source = path.read_text(encoding="utf-8")
node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_dashboard_html")
if isinstance(node.body[0], ast.Return) and isinstance(node.body[0].value, ast.Constant):
    template = root / "cbrs" / "web" / "overview.html"
    template.parent.mkdir(exist_ok=True)
    template.write_text(ast.literal_eval(node.body[0].value), encoding="utf-8")
    source = source.replace(ast.get_source_segment(source, node), '''def _dashboard_html() -> str:
    # UI edits appear on page refresh without changing the browser owner.
    return (Path(__file__).parent / "web" / "overview.html").read_text(encoding="utf-8")''')
    path.write_text(source, encoding="utf-8")
