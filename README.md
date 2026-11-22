# survivor: a Mech Royale bot

A `mechfight/1` bot that plays for placement (16 − place per match).

- **Navigation:** `nav.bin` is a 2 m walkability grid rasterized offline from the static map
  (`tools/build_nav.py`: buildings too tall to step onto, slopes over 50°, inflated by the mech
  radius). `nav.py` runs A* incrementally across decisions to stay inside the 50 ms budget, and
  smooths the path by line of sight. Stuck detection jumps and jets.
- **Zone:** heads for a point inside the *next* circle early, quietly (below the step-sound speed),
  and hurries only when the shrink is close or it is outside.
- **Encounters:** uses visible mech HP and distance to select a fallback when no immediate priority applies. It moves toward cube drops from eliminations and uses available boost for longer approaches.
- **Loot and upgrades:** values clusters of cubes and equipment by distance, remembers loot for seven seconds, and takes equipment within pickup range. Plating, gun damage and magazine size are the first upgrade choices when offered.

`tools/sim.py` is a small kinematic simulator used to test navigation and zone play.

Dependencies: Python standard library only (`mechfight.py` is the organizers' wrapper).

License: MIT.
