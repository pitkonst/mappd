"""A small pipeline process for tests of the runner and the CLI (no demo's weight).

The files under `demo_template/` are a minimal process: `$name` and `$Name` are substituted in
paths and contents, and the `.tmpl` suffix is dropped. Real processes start from the demos
(support, rfq, installation), not from this.
"""

from pathlib import Path
from string import Template

from mappd.process import register_process

TEMPLATE = Path(__file__).parent / "demo_template"


def camel(name: str) -> str:
    """site_visit -> SiteVisit."""
    return "".join(part.capitalize() for part in name.split("_"))


def make_process(processes_dir: Path, name: str) -> list[Path]:
    """Write processes_dir/<name> from the template and register it; the files written."""
    values = {"name": name, "Name": camel(name)}
    written: list[Path] = []
    for path in sorted(TEMPLATE.rglob("*.tmpl")):
        relative = Template(str(path.relative_to(TEMPLATE).with_suffix(""))).substitute(values)
        dest = processes_dir / name / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(Template(path.read_text()).substitute(values))
        written.append(dest)
    register_process(processes_dir, name)
    return sorted(written)
