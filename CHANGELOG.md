# Rumble Studio Changelog

## v0.15

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
