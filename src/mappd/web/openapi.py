"""Readable OpenAPI component names when two modules define a model of the same name.

FastAPI names such a component after its module path (`processes__rfq__schemas__Decision`), and
generated clients turn that into a class name. Each becomes its owner's name plus the model's
instead: `RfqDecision`, `SupportDecision`, `UsersRole`.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

REF = "#/components/schemas/"


def short_name(name: str) -> str:
    """`processes__rfq__schemas__Decision` → `RfqDecision`; a name without `__` stays."""
    parts = name.split("__")
    if len(parts) < 2:  # noqa: PLR2004 - a plain name
        return name
    owner = parts[1] if parts[0] == "processes" else parts[-2]
    return "".join(w.capitalize() for w in owner.split("_")) + parts[-1]


def _renamed(node: Any, names: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {
            k: (
                REF + names.get(v.removeprefix(REF), v.removeprefix(REF))
                if k == "$ref" and isinstance(v, str) and v.startswith(REF)
                else _renamed(v, names)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_renamed(v, names) for v in node]
    return node


def tidy(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema with module-path component names shortened and every `$ref` following.

    A short name that would collide with another component keeps its long name.
    """
    components: dict[str, Any] = schema.get("components", {}).get("schemas", {})
    names: dict[str, str] = {}
    taken = {n for n in components if "__" not in n}
    for name in sorted(components):
        short = short_name(name)
        if short != name and short not in taken:
            names[name] = short
            taken.add(short)
    if not names:
        return schema
    out: dict[str, Any] = _renamed(schema, names)
    out["components"]["schemas"] = {
        names.get(k, k): v for k, v in out["components"]["schemas"].items()
    }
    return out


def install(app: FastAPI) -> None:
    """Serve `app`'s OpenAPI schema with tidy component names."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = tidy(generate())
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
