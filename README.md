# survivor: a Mech Royale bot

A `mechfight/1` bot that plays for placement (16 − place per match).

- **Navigation:** `nav.bin` is a 2 m walkability grid rasterized offline from the static map
  (`tools/build_nav.py`: buildings too tall to step onto, slopes over 50°, inflated by the mech
  radius). `nav.py` runs A* incrementally across decisions to stay inside the 50 ms budget, and
  smooths the path by line of sight. Stuck detection jumps and jets.
- **Zone:** heads for a point inside the *next* circle early, quietly (below the step-sound speed),
  and hurries only when the shrink is close or it is outside.
- **Combat:** fights only attackers, mechs within 45 m, weak mechs nearby, and everyone at the end.
  Faces attackers (front armor), leads gun shots, uses lightning/laser when held, strafes, and
  backs off when losing.
- **Loot:** lightning and laser pickups, cubes and safe crates early; upgrades favour HP, armor and regen.

`tools/sim.py` is a small kinematic simulator used to test navigation and zone play.

Dependencies: Python standard library only (`mechfight.py` is the organizers' wrapper).

License: MIT.
