"""Audit DRF's bound output fields without dispatching a request or reading a row."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from inspect import getattr_static, getclosurevars, getsource, unwrap
from textwrap import dedent
from typing import TYPE_CHECKING, Any

from apps.ai.pending_outputs import PendingOutput, SealedOutput
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Model
from django.urls import URLResolver, get_resolver
from drf_spectacular.utils import OpenApiResponse
from rest_framework import serializers
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import ListModelMixin
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSetMixin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from django.urls import URLPattern
    from django.views import View

type Field = serializers.Field[Any, Any, Any, Any]
type Models = frozenset[type[Model]]
type Serializer = serializers.BaseSerializer[Any]


class UnresolvedSerializerError(AssertionError):
    """Static inspection cannot establish this route's field bindings."""


@dataclass(frozen=True)
class _MethodOnly:
    """The method is known; fabricating a user or request body would hide a branch."""

    method: str

    def __getattr__(self, attribute: str) -> object:
        msg = f"request.{attribute} is not statically known"
        raise UnresolvedSerializerError(msg)


def _models(value: object) -> Models:
    return (
        frozenset({value})
        if isinstance(value, type) and issubclass(value, Model)
        else frozenset()
    )


def _related(model: type[Model], attr: str) -> Models:
    # Django's reverse query name need not be the attribute DRF traverses
    # (e.g. careproposal vs careproposal_set). Never invoke a model property.
    try:
        field = model._meta.get_field(attr)
    except FieldDoesNotExist:
        return frozenset(
            r.related_model
            for r in model._meta.related_objects
            if r.get_accessor_name() == attr
        )
    if attr != field.name and attr == getattr(field, "attname", None):
        return frozenset()  # scalar FK id, not the related instance
    return _models(field.related_model)


def _source(field: Field, parents: Models, path: str, issues: set[str]) -> Models:
    for attr in field.source_attrs:
        for model in parents:
            if issubclass(model, (PendingOutput, SealedOutput)):
                issues.add(f"{path} -> {model.__name__}.{attr}")
        parents = frozenset(m for p in parents for m in _related(p, attr))
    return parents  # '*' has no source_attrs: the child receives the whole input


def _fields(
    field: Field,
    parents: Models,
    path: str,
    issues: set[str],
    ancestors: tuple[type, ...] = (),
) -> None:
    if isinstance(field, serializers.Serializer) and type(field) in ancestors:
        msg = f"recursive serializer at {path}"
        raise UnresolvedSerializerError(msg)
    chain = (*ancestors, type(field))
    if isinstance(
        field,
        (serializers.ListSerializer, serializers.ListField, serializers.DictField),
    ):
        if field.child is None:
            msg = f"missing child at {path}"
            raise UnresolvedSerializerError(msg)
        _fields(field.child, parents, path, issues, chain)
    elif isinstance(field, serializers.Serializer):
        parents |= _models(getattr(getattr(field, "Meta", None), "model", None))
        for name, bound in field.fields.items():
            child_path = f"{path}.{name}" if path else name
            reached = _source(bound, parents, child_path, issues)
            _fields(bound, reached, child_path, issues, chain)
    elif isinstance(field, serializers.BaseSerializer):
        msg = f"opaque serializer {type(field).__name__} at {path}"
        raise UnresolvedSerializerError(msg)
    else:
        issues.update(
            f"{path} -> {p.__name__}.*"
            for p in parents
            if issubclass(p, (PendingOutput, SealedOutput))
        )


def _as_serializers(value: object) -> Iterator[Serializer]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _as_serializers(item)
    elif isinstance(value, OpenApiResponse):
        yield from _as_serializers(value.response)
    elif isinstance(value, type) and issubclass(value, serializers.BaseSerializer):
        yield value()
    elif isinstance(value, serializers.BaseSerializer):
        yield value
    else:
        msg = f"expected a serializer, got {type(value).__name__}"
        raise UnresolvedSerializerError(msg)


def _manual_outputs(handler: Callable[..., object]) -> Iterator[Serializer]:
    # A @extend_schema response is not evidence about an actual `.data`
    # projection. Resolve the constructor on the handler itself, or refuse.
    fn = unwrap(handler)
    refs = getclosurevars(fn)
    names = {**refs.globals, **refs.nonlocals}
    tree = ast.parse(dedent(getsource(fn))).body[0]
    if not isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
        msg = "unknown manual serializer handler"
        raise UnresolvedSerializerError(msg)
    for stmt in tree.body:
        for access in ast.walk(stmt):
            if not isinstance(access, ast.Attribute) or access.attr != "data":
                continue
            expr = access.value
            if isinstance(expr, ast.Name) and expr.id == "request":
                continue  # inbound Request.data is not serializer output
            cls = (
                names.get(expr.func.id)
                if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
                else None
            )
            if not isinstance(cls, type) or not issubclass(
                cls, serializers.BaseSerializer
            ):
                msg = "cannot resolve manual serializer producing .data"
                raise UnresolvedSerializerError(msg)
            yield cls()


def _manual(view: object, action: str, method: str) -> Iterator[Serializer]:
    # Plain APIViews don't implement get_serializer. Inspect serializer objects
    # referenced by the bound handler, as well as its explicit per-method DTO
    # response contract (some handlers build dicts, not serializer instances).
    handler = getattr(view, action)
    schema_class = getattr(handler, "kwargs", {}).get("schema")
    concrete = list(_manual_outputs(handler))
    yield from concrete
    if schema_class is None:
        if concrete:
            return
        msg = "no output serializer class or manual DTO response contract"
        raise UnresolvedSerializerError(msg)
    if not concrete and (
        getattr(view, "queryset", None) is not None or hasattr(view, "get_queryset")
    ):
        msg = "manual response contract cannot resolve a model-bound serializer"
        raise UnresolvedSerializerError(msg)
    schema = schema_class()
    schema.view, schema.method = view, method.upper()
    yield from _as_serializers(schema.get_response_serializers())


def _selected(view: object, action: str, method: str) -> Iterator[Serializer]:
    getter = getattr(view, "get_serializer_class", None)
    direct = getattr(view, "serializer_class", None)
    factory = getattr(view, "get_serializer", None)
    many = isinstance(view, ListModelMixin) and action in {"list", "get", "head"}
    if factory is None and (getter is not None or direct is not None):
        # Never fall back to a documented or default class if selection fails.
        cls = getter() if getter is not None else direct
        if not isinstance(cls, type) or not issubclass(cls, serializers.BaseSerializer):
            msg = "serializer selector did not return a serializer class"
            raise UnresolvedSerializerError(msg)
        yield cls(
            many=many, context={"view": view, "request": getattr(view, "request", None)}
        )
    elif factory is not None:
        # Honor list_serializer_class, many_init, get_serializer overrides and
        # the same context used by the generic view's field builder.
        selected = factory(many=True) if many else factory()
        if not isinstance(selected, serializers.BaseSerializer):
            msg = "get_serializer did not return a serializer instance"
            raise UnresolvedSerializerError(msg)
        yield selected
    else:
        yield from _manual(view, action, method)


def _inputs(view: object) -> Models:
    query = getattr(view, "queryset", None)
    getter = getattr(view, "get_queryset", None)
    if (
        getter is not None
        and getattr(type(view), "get_queryset", None) is not GenericAPIView.get_queryset
    ):
        query = getter()
    model = _models(getattr(query, "model", None))
    if query is not None and not model:
        msg = "unresolved queryset model"
        raise UnresolvedSerializerError(msg)
    return model


def _routes(
    patterns: Iterable[URLPattern | URLResolver],
    prefix: str = "",
    defaults: Mapping[str, object] | None = None,
) -> Iterator[tuple[str, Callable[..., object], dict[str, object]]]:
    for pattern in patterns:
        path = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _routes(
                pattern.url_patterns,
                path,
                {**(defaults or {}), **pattern.default_kwargs},
            )
        else:
            yield path, pattern.callback, {**(defaults or {}), **pattern.default_args}


def _audit(view: object, action: str, method: str) -> set[str]:
    parents = _inputs(view)
    picked = list(_selected(view, action, method))
    if not picked:
        msg = "no statically resolvable serializer"
        raise UnresolvedSerializerError(msg)
    issues: set[str] = set()
    for serializer in picked:
        _fields(serializer, parents, "", issues)
    return issues


def _routed_actions(
    callback: Callable[..., object], cls: type[View], kwargs: Mapping[str, Any]
) -> Iterator[tuple[str, str]]:
    actions = getattr(callback, "actions", None)
    for method in kwargs.get("http_method_names", cls.http_method_names):
        action = actions.get(method) if actions is not None else method
        if method == "head" and not (action and hasattr(cls, action)):
            action = actions.get("get") if actions is not None else "get"
        if (
            action
            and hasattr(cls, action)
            and getattr_static(cls, action) is not APIView.options
        ):
            yield method, action


def _at_route(
    path: str, callback: Callable[..., object], defaults: dict[str, object]
) -> Iterator[str]:
    cls = getattr(callback, "cls", getattr(callback, "view_class", None))
    target = cls or callback
    protocol = ("serializer_class", "get_serializer_class", "get_serializer")
    if not any(hasattr(target, attr) for attr in protocol) and not (
        isinstance(cls, type) and issubclass(cls, APIView)
    ):
        return  # ordinary Django route with no DRF binding
    label = f"{path} {target.__qualname__}"
    try:
        if cls is None:
            for issue in sorted(_audit(callback, "", "")):
                yield f"{label}: {issue}"
            return
        kwargs = getattr(
            callback, "initkwargs", getattr(callback, "view_initkwargs", {})
        )
        actions = getattr(callback, "actions", None)
        methods = list(_routed_actions(callback, cls, kwargs))
        if not methods:
            yield f"{label}: unresolved serializer (no resolved output action)"
            return
        for method, action in methods:
            label = f"{path} {cls.__qualname__}.{action} ({method.upper()})"
            view = cls(**kwargs)
            if isinstance(view, ViewSetMixin):
                if not actions:
                    yield f"{label}: unresolved serializer (missing viewset action map)"
                    return
                view.action, view.action_map = action, actions
            vars(view).update(
                request=_MethodOnly(method.upper()), format_kwarg=None, kwargs=defaults
            )
            for issue in sorted(_audit(view, action, method)):
                yield f"{label}: {issue}"
    except Exception as exc:
        # A constructor, selector or field builder can fail for any reason.
        # Report the route and cause; another route's success cannot cover it.
        msg = f"{label}: unresolved serializer ({type(exc).__name__}: {exc})"
        raise UnresolvedSerializerError(msg) from exc


def resolved_serializer_violations() -> list[str]:
    found: list[str] = []
    for path, callback, defaults in _routes(get_resolver().url_patterns):
        try:
            found.extend(_at_route(path, callback, defaults))
        except UnresolvedSerializerError as exc:
            found.append(str(exc))
    return found
