RUMBLE STUDIO v0.13 — MUGEN MAPPING LOCKS
==========================================

WHAT'S NEW IN v0.13
-------------------
MUGEN rescans are now selective instead of all-or-nothing.

ANIMATION LOCKS
- Every Animation Mapping row now has a Lock checkbox.
- Locked Idle/Walk/Run/Hit/KO/Attack/etc. paths survive future MUGEN rescans.
- Lock all mapped / Unlock all buttons make it fast to freeze a good setup.

ATTACK LOCKS
- The attack table has a Lock column and a Lock / Unlock button.
- Locking an attack preserves its name, type, damage, range, cooldown, MUGEN action/state, and animation slot.
- Locking an attack also protects the animation slot it uses.

RESCAN UNLOCKED MUGEN
- The old rescan button is now ↻ RESCAN UNLOCKED MUGEN.
- Studio deep-scans DEF/AIR/CMD/CNS/ST again, but only replaces mappings that are NOT locked.
- Locked mappings are written to fighter.json immediately when you toggle them, so you can lock something and rescan without doing a full Save Fighter first.
- MUGEN_IMPORT_REPORT.txt records which slots were locked, preserved, and refreshed.

All v0.12 delete/reset/deep-scan tools, v0.11 Transformation Lab features, batch MUGEN import, DWC import, random rosters, transformations, and the battle/video engine remain included.


RUMBLE STUDIO v0.12 — FIGHTER MAINTENANCE + DEEP MUGEN RESCAN
===============================================================

WHAT'S NEW IN v0.12
-------------------
Fighter Editor now has three maintenance controls beside SAVE FIGHTER:

  ↻ RESCAN MUGEN
     Re-read an imported MUGEN fighter's DEF, AIR, CMD, CNS/ST and sprite files.
     The rescan rebuilds the MUGEN action library and attack mapping in-place while
     preserving saved stats, Stage/Form, facing, scale, AI, and transformations.
     If the original source moved, Studio asks you to locate the archive/DEF/folder again.

  RESET TO SAVED
     Throw away unsaved editor changes (and unsaved Lab changes for that fighter) and
     reload the last saved fighter.json.

  DELETE FIGHTER
     Permanently remove the fighter pack. Studio also removes it from the current roster
     and cleans saved transformation branches in other fighters that point to it.

DEEPER MUGEN ATTACK DETECTION
-----------------------------
v0.12 no longer relies only on generic AIR action-number ranges. It also scans CMD
ChangeState blocks and CNS/ST StateDefs, follows those state numbers to their AIR animation,
and ranks likely attacks. Obvious move names can also suggest melee/projectile/beam behavior.
The detailed results are written to each fighter's MUGEN_IMPORT_REPORT.txt.

All v0.11 Transformation Lab features, v0.10 transformations/batch MUGEN, DWC importing,
search/filter/random roster tools, and clean KO removal remain included.


RUMBLE STUDIO v0.11 — TRANSFORMATION LAB
=========================================

WHAT v0.11 ADDED
-----------------
The Transformation Lab lets you build and edit a whole evolution/form line from one screen.
You can click through connected forms in the Line Map, configure branching chances without
leaving the tab, and use Quick Linear Chain Builder for simple Pokemon/Digimon-style lines.

Examples:
  Agumon -> Greymon -> MetalGreymon -> WarGreymon
  Charmander -> Charmeleon -> Charizard
  Base Goku -> SSJ1 / SSJ2 / SSJ3 / God / Ultra Instinct

RUMBLE STUDIO v0.10 — TRANSFORMATIONS + BATCH MUGEN
===================================================

WHAT THIS BUILD ADDS

1. ACTIVE TRANSFORMATIONS / EVOLUTIONS
   Open a fighter, then use the Transformations tab to define one or more forms it can
   become after scoring an elimination. Every branch has its own percentage chance,
   optional heal amount, and announcement text.

   Example:
      Agumon -> Greymon       60%
             -> Tyrannomon    20%

   That gives Agumon an 80% overall chance to transform after each KO, with the remaining
   20% meaning no transformation yet. If branch totals reach 100% or more, a transformation
   is guaranteed and the percentages act as branch weights.

   Transforming preserves position, current target and elimination count. Current HP
   percentage carries into the new form, then the rule's optional Heal % is added. The new
   fighter pack supplies its own sprites, stats, attacks, scale and AI. The target form can
   have its own transformation rules, so long chains and branching trees work naturally.

2. TRANSFORMATION PRESENTATION
   Transformations create a visible gold pulse plus configurable announcement text while the
   new fighter sprite appears immediately. This works in Preview and rendered MP4 videos.

3. MASS MUGEN IMPORT
   The Fighter Library now has three MUGEN choices:
      ⚡ IMPORT MUGEN CHARACTER  - one character
      ⚡ BATCH MUGEN FILES       - select many .def/.zip/.rar/.7z files at once
      📁 SCAN MUGEN FOLDER       - point Studio at a collection/chars folder

   Batch imports ask whether to use:
      QUICK TEST - render only core auto-mapped AIR actions; much faster for big batches
      FULL       - preserve/render up to 600 AIR actions per fighter for later remapping

   A MUGEN_BATCH_IMPORT_REPORT.txt is written after every batch with successes and failures.

4. v0.9 QUALITY-OF-LIFE FEATURES REMAIN
   - defeated fighters fade and disappear from the arena
   - Fighter Library search + franchise + stage/form filters
   - Battle Roster search + franchise + stage/form filters
   - random X fighter selector
   - franchise-specific, stage-specific, and spread-across-franchises Multiverse random mode
   - DWC one-Digimon and full-ZIP import

BUNDLED TEST LIBRARY

- all 38 Rookie Digimon from the supplied Digimon World Championship archive
- Greymon (Champion), MetalGreymon (Ultimate), and WarGreymon (Mega) are also included as
  convenient transformation targets. No evolution rule is forced on Agumon; configure the
  chain yourself in the Transformations tab so normal battles stay under your control.

QUICK START

1. Double-click START_RUMBLE_STUDIO.bat
2. Select a fighter from Fighter Library.
3. Use Fighter Editor for sprites/stats/attacks/stage.
4. Use Transformations to add evolution/power-up branches.
5. Use Battle Roster to choose or randomly generate a cast.
6. Click PREVIEW BATTLE or RENDER VIDEO.

TRANSFORMATION EXAMPLES

Digimon:
   Agumon -> Greymon -> MetalGreymon -> WarGreymon
   A Rookie can also have several Champion targets with different chances.

Pokemon:
   Charmander -> Charmeleon -> Charizard

Dragon Ball:
   Goku Base -> Super Saiyan -> Super Saiyan 2 -> Super Saiyan 3 -> ...

Boss / action game:
   Phase 1 -> Phase 2

Each named form is simply another normal Rumble fighter pack.
