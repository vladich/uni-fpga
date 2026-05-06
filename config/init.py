#!/usr/bin/env python3
"""
Configuration loader.

A *configuration* (config/configurations/<id>.yml) is the unit of selection.
Each configuration declares a board + toolchain + a list of attached
peripherals. Picking a configuration resolves to a fully-loaded object that
synthesize.py and toolchain modules consume.
"""

import os
import sys
import logging

import yaml


log = logging.getLogger(__name__)
dir_path = os.path.dirname(os.path.realpath(__file__))


class ConfigError(Exception):
    """Raised when a configuration file is missing or malformed."""


def _load_yaml(path, root_key):
    if not os.path.exists(path):
        raise ConfigError("Config file not found: {p}".format(p=path))
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError("YAML parse error in {p}: {e}".format(p=path, e=exc))
    if data is None or root_key not in data:
        raise ConfigError("Missing root element '{k}' in {p}".format(k=root_key, p=path))
    return data[root_key]


def _load_yaml_dir(subdir, root_key, id_key):
    """Iterate config/<subdir>/*.yml and return {id: parsed_root}."""
    base = os.path.join(dir_path, subdir)
    if not os.path.isdir(base):
        raise ConfigError("Directory not found: " + base)
    out = {}
    for fname in sorted(os.listdir(base)):
        if not fname.endswith(".yml") or fname.startswith("_"):
            continue
        path = os.path.join(base, fname)
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ConfigError("YAML parse error in {p}: {e}".format(p=path, e=exc))
        if not data or root_key not in data:
            log.warning("Skipping %s — no '%s' root", path, root_key)
            continue
        item = data[root_key]
        if id_key not in item:
            log.warning("Skipping %s — no '%s' field", path, id_key)
            continue
        out[item[id_key]] = item
    return out


def read_toolchains():
    """Read the list of toolchains from toolchains.yml."""
    items = _load_yaml(os.path.join(dir_path, "toolchains.yml"), "Toolchains")
    return {t["Id"]: t for t in items}


def read_boards_catalog():
    """Read the master board catalog (config/boards.yml).

    Returns {board_id: catalog_entry} where each catalog_entry has BoardName,
    BoardProducer, PartProducer, PartFamily, Part. This is the index used for
    toolchain compatibility checks; the per-board pin map lives in
    config/boards/<id>.yml.
    """
    items = _load_yaml(os.path.join(dir_path, "boards.yml"), "Boards")
    return {b["Id"]: b for b in items}


# Back-compat alias.
read_boards = read_boards_catalog


def read_board_pinmap(board_id):
    """Load the per-board pin-map YAML (config/boards/<id>.yml).

    Returns the inner Board dict (with id, fpga, defaults, pinBanks) or None
    when the file is missing.
    """
    path = os.path.join(dir_path, "boards", board_id + ".yml")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError("YAML parse error in {p}: {e}".format(p=path, e=exc))
    return (data or {}).get("Board")


def read_parts():
    """Read the list of families and parts from parts.yml."""
    items = _load_yaml(os.path.join(dir_path, "parts.yml"), "Parts")
    parts_map = {}
    for part in items:
        family_map = {}
        for family in part.get("Families", []):
            if "Toolchains" in family:
                family_map[family["Family"]] = family["Toolchains"]
        parts_map[part["Producer"]] = family_map
    return parts_map


def read_peripherals():
    """Read every peripheral contract under config/peripherals/."""
    return _load_yaml_dir("peripherals", "Peripheral", "id")


def read_capabilities():
    """Read every capability contract under config/capabilities/."""
    return _load_yaml_dir("capabilities", "Capability", "id")


def read_configurations():
    """Read every configuration under config/configurations/."""
    return _load_yaml_dir("configurations", "Configuration", "id")


def is_compatible(boards, parts, board_id, toolchain_id):
    """
    Check if toolchain_id can synthesize for board_id.
    Returns True/False; raises ConfigError when the inputs themselves are invalid.
    """
    if board_id not in boards:
        raise ConfigError("Board {b} was not found in boards.yml".format(b=board_id))
    board = boards[board_id]
    producer = board["PartProducer"]
    family = board["PartFamily"]
    if producer not in parts or family not in parts[producer]:
        raise ConfigError(
            "PartFamily '{f}' under producer '{p}' (board '{b}') is not declared in parts.yml"
            .format(f=family, p=producer, b=board_id)
        )
    board_toolchains = parts[producer][family]
    if toolchain_id not in board_toolchains:
        log.error("Toolchain %s is not compatible with board %s", toolchain_id, board_id)
        return False
    return True


def resolve_configuration(configuration_id):
    """
    Look up a configuration by id and return a fully-resolved bundle:

        {
          "configuration": <Configuration dict>,
          "board":         <catalog entry from boards.yml>,
          "board_pinmap":  <pinBanks dict from config/boards/<id>.yml>,
          "toolchain":     <Toolchain dict from toolchains.yml>,
          "peripherals": [
              {"peripheral_id": ..., "peripheral": <Peripheral dict>,
               "params": {...}, "bind": {...}},
              ...
          ],
        }

    Raises ConfigError on any inconsistency.
    """
    configurations = read_configurations()
    if configuration_id not in configurations:
        raise ConfigError("Unknown configuration '{c}'. Run init_settings.py to pick one."
                          .format(c=configuration_id))
    cfg = configurations[configuration_id]

    boards = read_boards_catalog()
    toolchains = read_toolchains()
    parts = read_parts()
    peripherals = read_peripherals()

    board_id = cfg.get("board")
    toolchain_id = cfg.get("toolchain")
    if not board_id or not toolchain_id:
        raise ConfigError("Configuration '{c}' is missing board: or toolchain:".format(c=configuration_id))
    if board_id not in boards:
        raise ConfigError("Configuration '{c}' references unknown board '{b}'"
                          .format(c=configuration_id, b=board_id))
    if toolchain_id not in toolchains:
        raise ConfigError("Configuration '{c}' references unknown toolchain '{t}'"
                          .format(c=configuration_id, t=toolchain_id))
    if not is_compatible(boards, parts, board_id, toolchain_id):
        raise ConfigError("Configuration '{c}': toolchain '{t}' is not compatible with board '{b}'"
                          .format(c=configuration_id, t=toolchain_id, b=board_id))

    board_pinmap = read_board_pinmap(board_id)
    if board_pinmap is None:
        raise ConfigError("Per-board YAML config/boards/{b}.yml is missing — re-run "
                          "tools/curate_board.py".format(b=board_id))

    attached = []
    for entry in cfg.get("attach", []) or []:
        perip_id = entry.get("peripheral")
        if perip_id is None:
            raise ConfigError("Configuration '{c}': an attach entry has no peripheral"
                              .format(c=configuration_id))
        if perip_id not in peripherals:
            raise ConfigError("Configuration '{c}': unknown peripheral '{p}'"
                              .format(c=configuration_id, p=perip_id))
        attached.append({
            "peripheral_id": perip_id,
            "peripheral":    peripherals[perip_id],
            "params":        entry.get("params", {}) or {},
            "bind":          entry.get("bind", {}) or {},
        })

    return {
        "configuration": cfg,
        "board":         boards[board_id],
        "board_pinmap":  board_pinmap,
        "toolchain":     toolchains[toolchain_id],
        "peripherals":   attached,
    }


def read_all(configuration_id=None):
    """
    Read settings.yml + every other config file. Returns the fully-resolved
    configuration bundle, or None if no default has been set yet.
    """
    if configuration_id is None:
        settings_path = os.path.join(dir_path, "..", "settings.yml")
        if not os.path.exists(settings_path):
            return None
        try:
            with open(settings_path) as stream:
                settings = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            raise ConfigError("YAML parse error in settings.yml: {e}".format(e=exc))
        if not settings:
            return None
        configuration_id = settings.get("ConfigurationId")
        if configuration_id is None and "BoardId" in settings:
            raise ConfigError(
                "settings.yml uses the legacy {BoardId, Toolchain} format. "
                "Replace it with {ConfigurationId: <id>} or run init_settings.py "
                "to pick a configuration interactively."
            )

    if configuration_id is None:
        return None

    return resolve_configuration(configuration_id)


def _prompt_choice(prompt, count):
    raw = input(prompt)
    try:
        n = int(raw)
    except ValueError:
        raise ConfigError("Expected a number, got: {v!r}".format(v=raw))
    if not 1 <= n <= count:
        raise ConfigError("Number out of range (1..{c}): {n}".format(c=count, n=n))
    return n


def init():
    """
    Interactively pick a configuration in two stages: first a board, then a
    variant configuration on that board. Persist the choice to settings.yml
    and return the fully-resolved bundle.
    """
    configurations = read_configurations()
    if not configurations:
        raise ConfigError("No configurations found in config/configurations/")
    boards = read_boards_catalog()

    # Group configurations by board.
    by_board = {}
    for cfg_id, cfg in configurations.items():
        by_board.setdefault(cfg.get("board"), []).append((cfg_id, cfg))

    # Stage 1: pick a board.
    board_ids_with_configs = sorted(b for b in by_board if b in boards)
    print("\nAvailable boards (with configurations):\n")
    for i, bid in enumerate(board_ids_with_configs, start=1):
        b = boards[bid]
        print("  {i:3d}) {name}  ({producer} / {family}) — {n} configuration(s)".format(
            i=i,
            name=b.get("BoardName", bid),
            producer=b.get("PartProducer", "?"),
            family=b.get("PartFamily", "?"),
            n=len(by_board[bid]),
        ))
    n = _prompt_choice("\nEnter a board number: ", len(board_ids_with_configs))
    board_id = board_ids_with_configs[n - 1]

    # Stage 2: pick a configuration for that board.
    variants = sorted(by_board[board_id])
    if len(variants) == 1:
        cfg_id = variants[0][0]
        print("Only one configuration for this board: {}".format(cfg_id))
    else:
        print("\nConfigurations for {}:\n".format(boards[board_id].get("BoardName", board_id)))
        for i, (cfg_id, cfg) in enumerate(variants, start=1):
            tc = cfg.get("toolchain", "?")
            desc = cfg.get("description", "").strip()
            print("  {i:3d}) {id}  [toolchain: {tc}]".format(i=i, id=cfg_id, tc=tc))
            if desc:
                print("       {}".format(desc))
        n = _prompt_choice("\nEnter a configuration number: ", len(variants))
        cfg_id = variants[n - 1][0]

    # Validate by resolving once before saving.
    resolved = resolve_configuration(cfg_id)

    settings_path = os.path.join(dir_path, "..", "settings.yml")
    with open(settings_path, "w") as f:
        yaml.safe_dump({"ConfigurationId": cfg_id}, f)

    return resolved


def read_or_init(configuration_id=None):
    """
    Resolve a configuration: from the argument, settings.yml, or interactive prompt.
    """
    settings = read_all(configuration_id)
    if settings is not None:
        return settings
    return init()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        init()
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(1)
