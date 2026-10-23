"""Web helpers shared by every process's pages and API: forms, templates, HTTP pieces."""

from pathlib import Path

from mappd.web.forms import FormError, form, parse_form

STATIC = Path(__file__).parent / "static"

__all__ = ["STATIC", "FormError", "form", "parse_form"]
