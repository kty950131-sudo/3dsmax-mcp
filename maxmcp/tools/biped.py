"""Biped creation and BVH mocap import (kimodo/SMPL-X friendly)."""

from pathlib import Path

from maxmcp.helpers.bvh import (
    DEFAULT_BIPED_PRUNE,
    has_upright_spine,
    prepare_for_biped,
)
from maxmcp.helpers.maxscript import safe_string

from ..server import mcp, client


@mcp.tool()
def import_bvh_to_biped(
    bvh_path: str,
    biped_name: str = "",
    convert: bool = True,
) -> str:
    """Create a Biped and load a BVH motion capture file onto it.

    Args:
        bvh_path: Absolute path to a .bvh file.
        biped_name: Optional name for the biped root node (default: Bip auto-name).
        convert: Preprocess the BVH for Character Studio compatibility: strip a
            static dummy root above the hips, drop joints biped cannot map
            (eyes/jaw/fingers/end joints), and rename kimodo/SMPL-X joints to
            CS vocabulary (Spine1->Chest etc.). Writes `<stem>_biped.bvh` next
            to the source and loads that instead. kimodo exports need this;
            set False for files that already load natively.

    Returns the biped root name and resulting animation range, or an ERROR line.
    Warns when the rest skeleton is not Y-up (use the *_tpose export instead).
    """
    src = Path(bvh_path)
    if src.suffix.lower() != ".bvh":
        return f"ERROR: not a .bvh file: {bvh_path}"
    if not src.is_file():
        return f"ERROR: file not found: {bvh_path}"

    load_path = src
    warning = ""
    if convert:
        text = src.read_text(encoding="utf-8")
        try:
            converted = prepare_for_biped(text, prune=DEFAULT_BIPED_PRUNE)
            if not has_upright_spine(text):
                warning = (
                    " | WARNING: rest skeleton is not Y-up; poses will be "
                    "mangled. Use the *_tpose export of this clip."
                )
        except ValueError as exc:
            return f"ERROR: could not convert BVH: {exc}"
        load_path = src.with_name(src.stem + "_biped.bvh")
        load_path.write_text(converted, encoding="utf-8", newline="\n")

    ms_path = safe_string(str(load_path).replace("\\", "/"))
    rename_line = (
        f'bipObj.name = "{safe_string(biped_name)}"' if biped_name else ""
    )
    maxscript = f"""(
        local bipObj = biped.createNew 170 -90 [0,0,0]
        if bipObj == undefined then (
            "ERROR: biped.createNew failed"
        ) else (
            {rename_line}
            local oldQuiet = setQuietMode true
            local ok = false
            try ( ok = biped.loadMocapFile bipObj.transform.controller "{ms_path}" ) catch ()
            setQuietMode oldQuiet
            if ok then (
                "{{\\"biped\\": \\"" + bipObj.name + "\\", \\"animRange\\": \\"" + (animationRange as string) + "\\"}}"
            ) else (
                try ( delete bipObj ) catch ()
                "ERROR: loadMocapFile rejected the file: {ms_path}"
            )
        )
    )"""
    response = client.send_command(maxscript)
    return response.get("result", "") + warning
