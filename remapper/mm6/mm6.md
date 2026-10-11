# mm6
Implement `mm6.py`; a companion to `remapper.py` for keybind-related quality-of-life improvements specifically focused on playing `Might & Magic VI - The Mandate of Heaven`.

## Current State: Manual Cast-from-book
In MM6 there are limited ways to "quick cast" spells, and some spells inevitably must be cast manually from a characters spellbook, which is a comparatively tedious three-step process:

1. press the # corresponding to the caster's party position (1-4)
2. press B to open that player's spellbook
3. double-click on the location of the desired spell
4. (optionally) click to select a target, usually a player

## Future State: Keybound Autocast-from-book
The first quality-of-life feature I want mm6-caster.py to support is *automatic book-casting bound to a key*.

Three fundamental methods must exist for mm6-caster to support automatic book-casting:

1. `DEFINE` a bookcast: assign an obscure hotkey combination, e.g., CTRL-ALT-B to an assignment flow. This flow captures the a bookcast sequence: number, book key (B but just capture it in case I change it), (x,y) mouse coordinate of double-click, then (x,y) coordinate of any subsequent click. To complete the assignment, wait for the initiating keybind (CTRL-ALT-B).

2. `BIND` a bookcast: once the definition flow terminates, initiate a key-capture; the next key pressed by the user should be assigned to this bookcast sequence *assuming a CTRL modifier*. So if user presses `p`, the bookcast would be bound to `ctrl-p`.

3. `EXECUTE` a bookcast: once a definition exists and is bound to a key, mm6-caster.py should respond to the corresponding keybind by executing the predefined sequence: press the caster number, press the book key, double-click at the (x,y) location, optionally click on the target.

## Additional Considerations
Take the following into account when designing and implementing `mm6.py`.

### Borrow from pxlreact
Just like `remapper.py`, do not re-implement functions that can be inherited from the `pxlreact` repo under which this subproject exists. Keybinds, key captures, mouse coordinate handling, move-and-click, etc. should all be supported by `pxlreact` and its underlying `pyinterception` library.

### Configurable Delays
Each action in a bookcast sequence will need to be delayed by a certain number of miliseconds to allow for the operation to complete. The goal will be to find the *smallest* possible delay that still successfully executes the cast. 

This delay does not need to be available for definition or update during runtime, but allow easy configuration until an ideal "minimum viable value" is identified.

### Attach to Remapper (Optionally)
Don't make me run both `remapper.py` and `mm6.py` separately; both contribute to "keyboard and mouse quality of life." The difference is `mm6.py` is game specific, whereas `remapper.py` may be reused in the future for other similar oldschool DOS and PC gaming where rebinds are valuable.

### Future-future State
Do not build these features yet, but consider them in your planning:
- add `gates` to a bookcast which cast which can modify their behavior; for instance, a "Healing Touch" bookcast may select its target (step 4 click location) by finding the character with the lowest health
- create `repeatable until` bookcasts that run until a condition is met, for example a "Healing Touch" bookcast could repeat itself until all characters have reached a certain health threshold
- create `autobuffs` which recast critical party buffs when they expire, by periodically checking for their presence using on-screen pixel colors/locations to look for buff icons attached to character portraits
