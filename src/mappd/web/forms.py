"""HTML forms rendered from Pydantic models, and parsed back with per-field errors.

A process gets its approval form from its decision model: no template per field. Supported
field types: str (a textarea with `json_schema_extra={"widget": "textarea"}`), int, float,
bool, Literal and Enum (a select), `X | None` (empty means None), `list[str]` (one per line)
and nested models (a fieldset with dotted names). Anything else raises TypeError at render
time, so a process finds out in its page test.
"""

import types
import typing
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, get_args, get_origin

from markupsafe import Markup
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping

    from pydantic.fields import FieldInfo


_EMPTY = Markup("")


class FormError(ValueError):
    """A submitted form did not validate; `errors` maps dotted field paths to messages."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _resolve(tp: Any) -> tuple[Any, bool]:
    """The field's type with aliases and `| None` removed, and whether it was optional."""
    if isinstance(tp, typing.TypeAliasType):
        tp = tp.__value__
    if get_origin(tp) in (typing.Union, types.UnionType):
        args = get_args(tp)
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1 and len(args) == 2:  # noqa: PLR2004 - X | None
            inner, _ = _resolve(rest[0])
            return inner, True
    if isinstance(tp, typing.TypeAliasType):
        return _resolve(tp)
    return tp, False


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _is_lines(tp: Any) -> bool:
    return get_origin(tp) is list and get_args(tp) == (str,)


def _choices(tp: Any) -> list[str] | None:
    if get_origin(tp) is Literal:
        return [str(a) for a in get_args(tp)]
    if isinstance(tp, type) and issubclass(tp, Enum):
        return [str(m.value) for m in tp]
    return None


def _default(info: FieldInfo) -> Any:
    """The field's default, or None for a required field."""
    if info.is_required():
        return None
    return info.get_default(call_default_factory=True)


def _as_dict(values: BaseModel | Mapping[str, Any] | None) -> Mapping[str, Any]:
    if values is None:
        return {}
    if isinstance(values, BaseModel):
        return values.model_dump()
    return values


def form(
    model: type[BaseModel],
    values: BaseModel | Mapping[str, Any] | None = None,
    *,
    errors: Mapping[str, str] | None = None,
    hidden: Collection[str] = (),
    id_prefix: str = "f",
) -> Markup:
    """The form body for `model`: one labelled control per field, without the <form> tag.

    Controls get the ids `<id_prefix>-<field path>`; give each form on a page its own prefix,
    so a label never focuses another form's control.

    Raises:
        TypeError: a field has a type the renderer does not support (the message names it).
    """
    fields = _fields(model, _as_dict(values), errors or {}, set(hidden), "", id_prefix=id_prefix)
    return _EMPTY.join(fields)


def _fields(
    model: type[BaseModel],
    values: Mapping[str, Any],
    errors: Mapping[str, str],
    hidden: set[str],
    prefix: str,
    *,
    id_prefix: str,
) -> Iterator[Markup]:
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        value = values.get(name, _default(info))
        if isinstance(value, Enum):
            value = value.value
        if path in hidden:
            shown = "" if value is None else value
            yield Markup('<input type="hidden" name="{}" value="{}">').format(path, shown)
            if path in errors:  # no visible control to sit next to: name the field
                yield Markup('<span class="error">{}: {}</span>').format(path, errors[path])
            continue
        tp, optional = _resolve(info.annotation)
        label = info.title or name.replace("_", " ")
        if _is_model(tp):
            inner = _EMPTY.join(
                _fields(tp, _as_dict(value), errors, hidden, f"{path}.", id_prefix=id_prefix)
            )
            yield Markup("<fieldset><legend>{}</legend>{}</fieldset>").format(label, inner)
            continue
        control = _control(path, tp, optional, info, value, control_id=f"{id_prefix}-{path}")
        help_text = (
            Markup("<small>{}</small>").format(info.description) if info.description else _EMPTY
        )
        error = (
            Markup('<span class="error">{}</span>').format(errors[path])
            if path in errors
            else _EMPTY
        )
        yield Markup('<div class="field"><label for="{}-{}">{}</label>{}{}{}</div>').format(
            id_prefix, path, label, control, help_text, error
        )


def _control(
    path: str, tp: Any, optional: bool, info: FieldInfo, value: Any, *, control_id: str
) -> Markup:
    choices = _choices(tp)
    if choices is not None:
        options = [Markup('<option value=""></option>')] if optional else []
        for c in choices:
            selected = Markup(" selected") if value is not None and str(value) == c else _EMPTY
            options.append(Markup('<option value="{}"{}>{}</option>').format(c, selected, c))
        return Markup('<select id="{}" name="{}">{}</select>').format(
            control_id, path, _EMPTY.join(options)
        )
    if tp is bool:
        checked = Markup(" checked") if value else _EMPTY
        return Markup('<input type="checkbox" id="{}" name="{}" value="true"{}>').format(
            control_id, path, checked
        )
    if tp in (int, float):
        step = "1" if tp is int else "any"
        shown = "" if value is None else value
        return Markup('<input type="number" id="{}" name="{}" step="{}" value="{}">').format(
            control_id, path, step, shown
        )
    if _is_lines(tp):
        return Markup('<textarea id="{}" name="{}">{}</textarea>').format(
            control_id, path, "\n".join(value or [])
        )
    if tp is str:
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        shown = "" if value is None else value
        if extra.get("widget") == "textarea":
            return Markup('<textarea id="{}" name="{}">{}</textarea>').format(
                control_id, path, shown
            )
        return Markup('<input type="text" id="{}" name="{}" value="{}">').format(
            control_id, path, shown
        )
    raise TypeError(f"{path}: unsupported form field type {tp!r}")


def parse_form[M: BaseModel](model: type[M], data: Mapping[str, str | list[str]]) -> M:
    """Validate submitted form `data` (flat, dotted names) into `model`.

    Raises:
        FormError: per-field messages, keyed by dotted path.
    """
    try:
        return model.model_validate(_collect(model, data, ""))
    except ValidationError as e:
        errors = {".".join(str(p) for p in err["loc"]) or "form": err["msg"] for err in e.errors()}
        raise FormError(errors) from e


def _first(raw: str | list[str] | None) -> str | None:
    if isinstance(raw, list):
        return raw[0] if raw else None
    return raw


def _collect(
    model: type[BaseModel], data: Mapping[str, str | list[str]], prefix: str
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        tp, optional = _resolve(info.annotation)
        if _is_model(tp):
            out[name] = _collect(tp, data, f"{path}.")
            continue
        raw = _first(data.get(path))
        if tp is bool:
            out[name] = raw not in (None, "", "false")
        elif _is_lines(tp):
            out[name] = [line.strip() for line in (raw or "").splitlines() if line.strip()]
        elif raw is None:
            continue  # the model's default, or its "Field required"
        elif raw == "" and optional:
            out[name] = None
        else:
            out[name] = raw
    return out
