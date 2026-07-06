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
            if config.get("dp_id") is not None:
                dp_ids.add(config["dp_id"])
    return dp_ids
