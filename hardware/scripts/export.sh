#!/usr/bin/env bash
# Regenerate the documentation outputs from the KiCad sources.
# Requires KiCad 9 (kicad-cli on PATH) and Python 3. Run from the hardware/ directory.
set -euo pipefail

SRC=kicad/main-board
NAME=TIMECODE-MASTER-Master
OUT=docs
TMP=$(mktemp -d)

kicad-cli sch export pdf  -o "$OUT/schematic.pdf" "$SRC/$NAME.kicad_sch"
kicad-cli sch export svg --no-background-color -o "$TMP" "$SRC/$NAME.kicad_sch"
mv "$TMP/$NAME.svg" "$OUT/img/schematic.svg"

kicad-cli pcb render --side top    --width 2400 --height 1700 --quality high --background opaque \
    -o "$OUT/img/board-top.png"    "$SRC/$NAME.kicad_pcb"
kicad-cli pcb render --side bottom --width 2400 --height 1700 --quality high --background opaque \
    -o "$OUT/img/board-bottom.png" "$SRC/$NAME.kicad_pcb"

# 2D layout views as SVG (docs/img/board-layout-*.png and copper-*.png were rasterised from these):
for spec in "layout-front:F.Cu,F.Silkscreen,F.Mask,Edge.Cuts" "layout-back:B.Cu,B.Silkscreen,B.Mask,Edge.Cuts:--mirror"             "copper-front:F.Cu,Edge.Cuts" "copper-inner1:In1.Cu,Edge.Cuts" "copper-inner2:In2.Cu,Edge.Cuts"             "copper-back:B.Cu,Edge.Cuts:--mirror"; do
    IFS=: read -r name layers extra <<< "$spec"
    kicad-cli pcb export svg --layers "$layers" --exclude-drawing-sheet --page-size-mode 2 ${extra:-}         -o "$OUT/img/$name.svg" "$SRC/$NAME.kicad_pcb"
done

kicad-cli sch export netlist --format kicadxml -o "$TMP/net.xml" "$SRC/$NAME.kicad_sch"
python scripts/make_bom.py "$TMP/net.xml" bom.csv

# Fabrication outputs for a new order (written to a scratch folder, not into the repository):
#   kicad-cli pcb export gerbers --board-plot-params -l F.Cu,In1.Cu,In2.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts -o fab/ "$SRC/$NAME.kicad_pcb"
#   kicad-cli pcb export drill --format excellon --excellon-separate-th -o fab/ "$SRC/$NAME.kicad_pcb"

rm -rf "$TMP"
