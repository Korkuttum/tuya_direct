"""Build generic Home Assistant entity mappings from Tuya DP metadata."""
from __future__ import annotations

import base64
import json
import re
import struct
from typing import Any


EMPTY_MAPPING: dict[str, Any] = {
    "sensors": {},
    "binary_sensors": {},
    "switches": {},
    "numbers": {},
    "selects": {},
    "model_id": "generic",
    "model_name": "Generic Tuya Device",
}


def create_empty_mapping(model_id: str | None = None, model_name: str | None = None) -> dict[str, Any]:
    """Create a blank dynamic mapping."""
    mapping = {key: dict(value) if isinstance(value, dict) else value for key, value in EMPTY_MAPPING.items()}
    mapping["model_id"] = model_id or "generic"
    mapping["model_name"] = model_name or "Generic Tuya Device"
    return mapping


def build_mapping_from_tuya_model(model_info: dict[str, Any] | None) -> dict[str, Any]:
    """Create a model mapping from Tuya /model response data."""
    model_info = model_info or {}
    model_id = model_info.get("modelId") or model_info.get("model_id") or "generic"
    model_name = model_info.get("name") or model_info.get("modelName") or f"Tuya Direct {model_id}"
    mapping = create_empty_mapping(model_id, model_name)

    for item in _iter_properties(model_info):
        config = _entity_config_from_property(item)
        if not config:
            continue
        bucket, code, entity_config = config
        mapping[bucket][code] = entity_config

    return mapping


def extend_mapping_from_data(mapping: dict[str, Any], data: dict[str, Any] | None) -> dict[str, Any]:
    """Add missing DPs and decoded raw fields from current device data."""
    if not data:
        return mapping

    existing_codes = _all_codes(mapping)
    existing_dp_ids = _all_dp_ids(mapping)

    for code, item in data.items():
        value = item.get("value") if isinstance(item, dict) else item
        dp_id = item.get("dp_id") if isinstance(item, dict) else None
        value_type = (item.get("type") or "").lower() if isinstance(item, dict) else ""

        if code not in existing_codes and dp_id not in existing_dp_ids:
            bucket, entity_code, entity_config = _entity_config_from_value(code, dp_id, value, value_type)
            mapping[bucket][entity_code] = entity_config
            existing_codes.add(entity_code)
            if dp_id is not None:
                existing_dp_ids.add(dp_id)

        if value_type == "raw" or _looks_like_base64_payload(value):
            _add_raw_field_sensors(mapping, code, dp_id, value)

    return mapping


def _iter_properties(model_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return property definitions from known Tuya model shapes."""
    candidates: list[dict[str, Any]] = []

    for key in ("properties", "status", "functions"):
        value = model_info.get(key)
        if isinstance(value, list):
            candidates.extend(value)

    services = model_info.get("services")
    if isinstance(services, list):
        for service in services:
            for key in ("properties", "status", "functions"):
                value = service.get(key)
                if isinstance(value, list):
                    candidates.extend(value)

    return candidates


def _entity_config_from_property(prop: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    dp_id = _first_existing(prop, "abilityId", "dpId", "dp_id", "id")
    code = _safe_code(prop.get("code"), dp_id)
    name = prop.get("name") or _title_from_code(code)
    access = str(_first_existing(prop, "accessMode", "access_mode", "mode") or "ro").lower()
    writable = "w" in access

    type_spec = _parse_values(prop.get("typeSpec") or prop.get("values") or {})
    dp_type = str(type_spec.get("type") or prop.get("type") or prop.get("valueType") or "").lower()

    base_config = {
        "dp_id": dp_id,
        "code": code,
        "name": name,
        "conversion": "value",
    }

    if dp_type == "bool":
        if writable:
            return "switches", code, base_config
        return "binary_sensors", code, {**base_config, "conversion": "value in [1, True, '1', 'true', 'on', 'yes', 'enable', 'open']"}

    if dp_type == "enum":
        options = _enum_options(type_spec)
        if writable:
            return "selects", code, {**base_config, "options": options, "values": type_spec}
        return "sensors", code, {**base_config, "values": type_spec, "icon": "mdi:format-list-bulleted"}

    if dp_type in ("value", "integer", "float", "double"):
        numeric_config = _numeric_config(base_config, type_spec)
        if writable:
            return "numbers", code, numeric_config
        return "sensors", code, {**numeric_config, "state_class": "measurement"}

    if dp_type == "raw":
        return "sensors", code, {**base_config, "icon": "mdi:code-braces", "entity_category": "diagnostic"}

    return "sensors", code, {**base_config, "icon": "mdi:information-outline"}


def _entity_config_from_value(code: str, dp_id: int | None, value: Any, value_type: str) -> tuple[str, str, dict[str, Any]]:
    entity_code = _safe_code(code, dp_id)
    base_config = {
        "dp_id": dp_id,
        "code": entity_code,
        "name": _title_from_code(entity_code),
        "conversion": "value",
    }
    if isinstance(value, bool) or value_type == "bool":
        return "binary_sensors", entity_code, {
            **base_config,
            "conversion": "value in [1, True, '1', 'true', 'on', 'yes', 'enable', 'open']",
        }
    if isinstance(value, (int, float)) or value_type in ("value", "integer", "float", "double", "int"):
        return "sensors", entity_code, {**base_config, "state_class": "measurement"}
    return "sensors", entity_code, base_config


def _numeric_config(base_config: dict[str, Any], type_spec: dict[str, Any]) -> dict[str, Any]:
    scale = _as_number(type_spec.get("scale"), 0)
    factor = 10 ** int(scale) if scale else 1
    config = dict(base_config)

    if factor != 1:
        config["conversion"] = f"value / {factor}"
        config["api_conversion"] = f"round(value * {factor})"

    unit = type_spec.get("unit")
    if unit:
        config["unit"] = unit

    for source, target in (("min", "min_value"), ("max", "max_value"), ("step", "step")):
        if source in type_spec:
            raw = _as_number(type_spec[source], None)
            if raw is not None:
                config[target] = raw / factor if factor else raw

    return config


def _enum_options(type_spec: dict[str, Any]) -> dict[str, str]:
    raw_range = type_spec.get("range") or type_spec.get("ranges") or type_spec.get("options") or []
    if isinstance(raw_range, dict):
        return {str(key): str(value) for key, value in raw_range.items()}
    if isinstance(raw_range, list):
        return {str(item): _title_from_code(str(item)) for item in raw_range}
    return {}


def _add_raw_field_sensors(mapping: dict[str, Any], raw_code: str, dp_id: int | None, value: Any) -> None:
    if not isinstance(value, str):
        return
    try:
        payload = base64.b64decode(value)
    except Exception:
        return

    if not payload:
        return

    if len(payload) % 4 == 0:
        field_count = len(payload) // 4
        encoding = "int32_be"
    elif len(payload) % 2 == 0:
        field_count = len(payload) // 2
        encoding = "int16_be"
    else:
        field_count = len(payload)
        encoding = "uint8"

    for index in range(field_count):
        code = f"rawdp{dp_id or raw_code}index{index + 1}"
        if code in mapping["sensors"]:
            continue
        mapping["sensors"][code] = {
            "dp_id": dp_id,
            "code": code,
            "name": f"Raw DP {dp_id or raw_code} Index {index + 1}",
            "raw_source": raw_code,
            "field_index": index,
            "encoding": encoding,
            "conversion": "value",
            "icon": "mdi:code-array",
        }


def _looks_like_base64_payload(value: Any) -> bool:
    if not isinstance(value, str) or len(value) < 8:
        return False
    try:
        payload = base64.b64decode(value, validate=True)
    except Exception:
        return False
    if len(payload) < 2:
        return False
    try:
        struct.unpack_from("B", payload, 0)
        return True
    except Exception:
        return False


def _parse_values(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _safe_code(code: Any, dp_id: Any) -> str:
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", code):
        return code
    return f"dp_{dp_id}" if dp_id is not None else "dp_unknown"


def _title_from_code(code: str) -> str:
    return code.replace("_", " ").strip().title() or code


def _first_existing(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def _as_number(value: Any, default: Any) -> Any:
    try:
        if value is None:
            return default
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return default


def _all_codes(mapping: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for bucket in ("sensors", "binary_sensors", "switches", "numbers", "selects"):
        codes.update(mapping.get(bucket, {}).keys())
    return codes


def _all_dp_ids(mapping: dict[str, Any]) -> set[Any]:
    dp_ids: set[Any] = set()
    for bucket in ("sensors", "binary_sensors", "switches", "numbers", "selects"):
        for config in mapping.get(bucket, {}).values():
            if "dp_id" in config:
                dp_ids.add(config["dp_id"])
    return dp_ids
