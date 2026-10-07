# Rumble Studio Changelog

## v0.16.1

### Final Fantasy Brave Exvius Sprite Import

- Added direct import for The Spriters Resource FFBE character ZIPs.
- Detects multiple rarity/form folders and lets the user choose one, several, or ALL.
- Splits FFBE three-column PNG sprite sheets into transparent animated GIFs automatically.
- Auto-maps idle, movement, hit, guard, KO, attack, magic, Limit Burst, entrance, and victory animations where present.
- Preserves all recognized source PNG sheets inside each local fighter folder for later remapping.
- Creates placeholder Attack, Magic, Limit Burst, and optional Special move definitions that can be edited in Fighter Editor.
- Defaults FFBE fighters to the Final Fantasy franchise and a 1.6 sprite scale.


## v0.16
- Fixed Battle Presentation Director crash caused by missing Python `re` import in special-move classification.

### Battle Presentation Director

- Added adaptive HUD densities for small, medium, and giant rumbles.
- Added a dedicated FINAL TWO head-to-head HUD that removes defeated-roster clutter.
- Added a live remaining-fighter counter during combat.
- Added survivor milestone banners for 32, Final 16, Final 8, Final 4, and Final Two when applicable.
- Added a compact kill feed showing recent eliminations and transformations.
- KO presentation now shows the defeated fighter's name in normal-size battles.
- Existing transformations remain visible near the fighter and now also appear in the event feed.


## v0.15
- Transformation Rule targets are now reliably restricted to the source fighter's franchise using the source pack directly.

### Battle Opening & Spawn Director

- Added fair circular starting formations and automatic two-ring layouts for larger rumbles.
- Fighters enter clockwise beginning at 12 o'clock instead of appearing randomly.
- Added Digital Beam, Flash, Aura, Portal, Teleport, and None entrance effects.
- Added WHO WILL WIN? / CHOOSE YOUR FIGHTER presentation, 3-2-1 countdown, and RUMBLE! release.
- AI combat is frozen until the opening finishes.
- Added a per-fighter Entrance animation slot in Fighter Editor and MUGEN animation mapping.
- Added Entrance Studio for shared entrance effects by franchise and/or Stage/Form.
- Digimon defaults to Digital Beam, Pokemon to Flash, Dragon Ball to Aura, and Kingdom Hearts to Portal unless overridden.
- Battle Roster now has opening controls for effect, delay, hold time, countdown, and formation.
- WHO WILL WIN? now sizes itself inside the empty center of the spawn formation so it does not cover the 3/9 o'clock fighters.
- Added MUGEN Smart Scan: parses real `[Command]` inputs and traces them through State -1 / StateDef to playable AIR animations.
- Move Lab now defaults to Recommended, with separate Inputs/attacks, Standard actions, and All rendered views.
- Move Lab shows semantic MUGEN inputs such as X, QCF + X, Dash F, and charge motions instead of forcing you to dig through hundreds of helper animations.


## v0.14

### MUGEN Move Lab

- Added a dedicated MUGEN Move Lab tab.
- Expanded Move Lab into a full animation mapper: any rendered AIR action can be assigned to Idle, Walk, Run, Hit, Guard, KO, Attack 1-4, Jump, Happy, Cheer, Victory Jump, or Dedicated Victory.
- General animation mapping does not overwrite combat damage/range/cooldown; Attack 1-4 quick buttons still configure combat moves.
- Browse/search/filter every rendered AIR action.
- Preview MUGEN moves directly in Studio.
- Assign any detected action to Attack 1-4.
- Optional lock-on-assign keeps chosen mappings stable through future rescans.
- Move assignments persist immediately without rewriting unrelated fighter settings.
- MUGEN imports/rescans now write `MUGEN_MOVE_INDEX.json` with move names, AIR actions, StateDefs, inferred attack types, detection source, and confidence scores.
- Older v0.13 imports remain browseable and can be rescanned once for richer metadata.


## v0.13

Current GitHub baseline.

### Major systems already implemented

- Autonomous free-for-all sprite battles
- HP, damage, knockouts, and victory detection
- Melee, projectile, and beam attacks
- Fighter AI profiles
- Sprite orientation handling
- KO fade/removal
- Animated winner presentation
- Vertical MP4 rendering
- Universal fighter format
- Digimon World Championship bulk import
- MUGEN SFF v1/v2 import
- Batch MUGEN importing
- MUGEN rescanning
- MUGEN animation and attack locks
- Fighter search and filtering
- Stage/form metadata
- Random roster generation
- Multiverse roster generation
- Transformation Lab
- Branching transformations

## Next Planned

- MUGEN Move Lab
- Shorts / DigiRumble-style presentation
- Attack visual effects and sound
- Arena system
- Expanded combat behaviors
