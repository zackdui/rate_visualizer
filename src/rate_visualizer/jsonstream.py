"""Stream a top-level JSON object one piece at a time, without caring about key order.

Yields, in file order:
  ("key",   key, None, None)    each top-level key as it starts
  ("value", key, None, value)   a top-level value that is not in list_keys (fully built)
  ("item",  key, index, value)  each element of a top-level list named in list_keys (fully built)
"""
import ijson

_OPEN = ("start_map", "start_array")
_CLOSE = ("end_map", "end_array")


def _build(first_event, first_value, events):
    """Build one complete value that starts with (first_event, first_value)."""
    if first_event not in _OPEN:
        return first_value
    b = ijson.ObjectBuilder()
    b.event(first_event, first_value)
    depth = 1
    for _, event, value in events:
        b.event(event, value)
        if event in _OPEN:
            depth += 1
        elif event in _CLOSE:
            depth -= 1
            if depth == 0:
                return b.value
    raise ValueError("JSON ended inside a value")


def iter_top_level(fh, list_keys):
    events = ijson.parse(fh)  # numbers come back as Decimal, so exact text is preserved
    key, index = None, 0
    for prefix, event, value in events:
        if prefix == "" and event == "map_key":
            key, index = value, 0
            yield "key", key, None, None
        elif prefix == key and key in list_keys and event == "start_array":
            continue  # items follow with prefix "<key>.item"
        elif prefix == key and key in list_keys and event == "end_array":
            continue
        elif prefix == f"{key}.item" and key in list_keys:
            yield "item", key, index, _build(event, value, events)
            index += 1
        elif prefix == key:
            yield "value", key, None, _build(event, value, events)
