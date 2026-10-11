# Improvements/Fixes: mm6 and bookcast
The bookcast feature works well, but immediate improvements are evident.

## Conditional Caster Select
The "select caster" step (initial number key) must be made optional upon condition that this character is not already selected. If the caster is already the active character, pressing this number again opens their character stats screen which prevents the rest of the bookcast from proceeding.

### One-time Data Capture: Unselected Colors
Implement a singular, throw-away module `capture-unselected.py` with a one-time purpose: capture the next four left-click events, recording the (x,y) position of the mouse and the color of the pixel under the cursor at that location and time.

These data must be recorded such that their sequence is assigned a corresponding numeral 1-4, each number corresponding to an (x,y) coordinate and color set.

### "Already Selected" Check
The four (x,y) coordinate-color pairs represent the state of an *unselected* character. The "selection" halo pulses, so its color cannot be accurately predicted. Instead, bookcasts will perform a negative check:
- for the caster associated with this bookcast, check the color of the pixel at the corresponding (x,y) in the unselected data
- if the color matches, the character is not currently selected, so send the selection number

### Reframing
Put another way, bookcasts must skip the first step (select caster) if that caster is already active. To determine this, `mm6.py` will use predefined unselected pixel coordinates and colors to verify selection status before sending the keypress (skipping it otherwise).

## Magic School Selection
An addition step is required in both the `define` and `execute` stages of bookcasting. Characters have different schools of magic, and the (x,y) click coordinates of a spell depend upon the corresponding school being active.

As such, each bookcast must add a step after opening the spellbook: `select school`. This is captured as a single left-click event.

## New Bookcast Flow
Taken together, the above improvements create a new flow for the `DEFINE` flow:

1. capture the # of the caster (*no change*)
2. capture the book key (*no change*)
3. capture the mouse (x,y) coordinate of the magic school (**new**)
4. capture the mouse (x,y) coordinate of the spell (*no change*)
5. (*optional*) capture the mouse (x,y) coordinate of the target (*no change*)

And likewise the `EXECUTE` flow has new logic:

1. check the color of the pixel at the (x,y) coordinate associated with the unselected-check of the caster (**new**)
2. *if* the pixel color matches the unselected color, press the caster key (**modified**)
3. press the book key (*no change*)
4. click the magic school (**new**)
5. double-click the spell (*no change*)
6. (*optional*) click the target (*no change*)
