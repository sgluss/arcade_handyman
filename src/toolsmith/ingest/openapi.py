"""Deterministic OpenAPI 3.x -> APISpec.

No LLM involvement: parsing a machine-readable spec is a solved problem, and
keeping this stage deterministic means ingest bugs are ordinary bugs, not
prompt issues. The parser is deliberately shallow — it flattens each operation
to the facts tool design needs (params, descriptions, a response-shape hint)
rather than modeling all of OpenAPI.
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from toolsmith.ir import APIParam, APISpec, AuthScheme, Endpoint

# Identify ourselves to spec hosts (api.weather.gov rejects anonymous clients).
_FETCH_HEADERS = {"User-Agent": "toolsmith (https://github.com/sgluss)", "Accept": "*/*"}

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# JSON-ish response content types, in preference order for the response hint.
_JSON_CONTENT_TYPES = ("application/json", "application/geo+json", "application/ld+json")


def spec_from_openapi(source: str) -> APISpec:
    """Parse an OpenAPI document (URL or local path, JSON or YAML) into an APISpec."""
    doc = _load_document(source)
    resolve = _make_resolver(doc)

    info = doc.get("info", {})
    servers = doc.get("servers", [])
    base_url = servers[0]["url"] if servers else _origin_of(source)

    endpoints = []
    for path, path_item in doc.get("paths", {}).items():
        shared_params = path_item.get("parameters", [])
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if op is None:
                continue
            endpoints.append(_endpoint(method, path, op, shared_params, resolve))

    return APISpec(
        name=info.get("title", "api"),
        base_url=base_url.rstrip("/"),
        description=_squash(info.get("description", "")),
        auth=_auth_schemes(doc, resolve),
        endpoints=endpoints,
    )


# --- document loading -------------------------------------------------------


def _load_document(source: str) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        text = httpx.get(source, headers=_FETCH_HEADERS, follow_redirects=True, timeout=30).text
    else:
        text = Path(source).read_text()
    # YAML is a superset of JSON, but try JSON first for exact error messages.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return yaml.safe_load(text)


def _origin_of(source: str) -> str:
    match = re.match(r"(https?://[^/]+)", source)
    return match.group(1) if match else ""


# --- $ref resolution --------------------------------------------------------


def _make_resolver(doc: dict[str, Any]):
    """Return resolve(node) that follows local '#/...' refs, cycle-guarded."""

    def resolve(node: Any, _depth: int = 0) -> Any:
        while isinstance(node, dict) and "$ref" in node and _depth < 10:
            pointer = node["$ref"].lstrip("#/").split("/")
            node = doc
            for key in pointer:
                node = node.get(key, {})
            _depth += 1
        return node

    return resolve


# --- endpoints --------------------------------------------------------------


def _endpoint(method: str, path: str, op: dict, shared_params: list, resolve) -> Endpoint:
    params = []
    for raw in [*shared_params, *op.get("parameters", [])]:
        parsed = _param(raw, resolve)
        if parsed is not None:
            params.append(parsed)

    return Endpoint(
        id=op.get("operationId") or _slug(f"{method}_{path}"),
        method=method.upper(),
        path=path,
        description=_squash(op.get("description") or op.get("summary") or ""),
        params=params,
        response_hint=_response_hint(op, resolve),
    )


def _param(raw: dict, resolve) -> APIParam | None:
    param = resolve(raw)
    if not isinstance(param, dict) or "name" not in param:
        return None
    location = param.get("in", "query")
    if location not in ("query", "path", "header"):
        return None  # cookie params etc. are out of scope

    schema = resolve(param.get("schema", {}))
    param_type = schema.get("type", "string")
    if param_type not in ("string", "integer", "number", "boolean", "array"):
        param_type = "string"

    enum = schema.get("enum") or resolve(schema.get("items", {})).get("enum") or []
    description = _squash(param.get("description") or schema.get("description") or "")
    if param.get("deprecated"):
        description = f"(deprecated) {description}"

    return APIParam(
        name=param["name"],
        location=location,
        type=param_type,
        required=bool(param.get("required")) or location == "path",
        description=description,
        enum=[str(v) for v in enum[:12]],
    )


def _response_hint(op: dict, resolve) -> str:
    """Shallow shape of the success response — enough for the design stage to
    choose a response_extract path, without shipping whole schemas to the LLM."""
    responses = op.get("responses", {})
    success = resolve(responses.get("200") or responses.get("201") or {})
    content = success.get("content", {})
    for content_type in [*_JSON_CONTENT_TYPES, *content.keys()]:
        if content_type in content:
            schema = resolve(content[content_type].get("schema", {}))
            return _shape(schema, resolve, depth=2)[:300]
    return ""


def _shape(schema: dict, resolve, depth: int) -> str:
    """Render a schema as a compact '{a, b: {...}}' sketch, `depth` levels deep."""
    schema = _flatten_composition(resolve(schema), resolve)
    if schema.get("type") == "array" or "items" in schema:
        return f"[{_shape(schema.get('items', {}), resolve, depth)}]"
    properties = schema.get("properties")
    if not properties:
        return schema.get("type", "object")
    if depth <= 0:
        return "{...}"
    parts = []
    for name, child in list(properties.items())[:12]:
        child = resolve(child)
        if child.get("properties") or "items" in child:
            parts.append(f"{name}: {_shape(child, resolve, depth - 1)}")
        else:
            parts.append(name)
    return "{" + ", ".join(parts) + "}"


def _flatten_composition(schema: dict, resolve) -> dict:
    """Merge allOf variants and pick the first informative anyOf/oneOf variant,
    so wrapper patterns (e.g. GeoJSON feature + payload) still yield a shape."""
    if "allOf" in schema:
        merged: dict[str, Any] = {"properties": {}}
        for variant in schema["allOf"]:
            variant = _flatten_composition(resolve(variant), resolve)
            merged["properties"].update(variant.get("properties", {}))
        return merged
    for key in ("anyOf", "oneOf"):
        if key in schema:
            for variant in schema[key]:
                variant = _flatten_composition(resolve(variant), resolve)
                if variant.get("properties") or "items" in variant:
                    return variant
    return schema


# --- auth -------------------------------------------------------------------


def _auth_schemes(doc: dict, resolve) -> list[AuthScheme]:
    schemes = []
    for scheme in doc.get("components", {}).get("securitySchemes", {}).values():
        scheme = resolve(scheme)
        kind = scheme.get("type")
        if kind == "apiKey" and scheme.get("in") in ("header", "query"):
            schemes.append(
                AuthScheme(
                    kind="api_key",
                    name=scheme["name"],
                    location=scheme["in"],
                    description=_squash(scheme.get("description", "")),
                )
            )
        elif kind == "http":
            schemes.append(
                AuthScheme(
                    kind="http_bearer",
                    name="Authorization",
                    description=_squash(scheme.get("description", "")),
                )
            )
        elif kind == "oauth2":
            schemes.append(
                AuthScheme(kind="oauth", name="Authorization",
                           description=_squash(scheme.get("description", "")))
            )
    return schemes


# --- text helpers -----------------------------------------------------------


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
