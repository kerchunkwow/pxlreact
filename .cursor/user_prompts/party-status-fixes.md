# Party Status & Behavior Still Broken
Despite your efforts and claims of perfect detection, functionality is still frustratingly incomplete.

Validate against the live game:

- mm8.exe is open and active
- party is still `3` players, player 1 is `selected`
- all players are `ready`
- **no** player is at 100% health, health should read *approximately* `~85%` for p1, `~50%` for p2, `~80%` for p3.


## Caster Reselection
`ctrl-b` is currently bound as a *bookcast* for *p2*. It works when p2 is *not* selected, so keys and coordinates are accurate. But if p2 is *already selected*, it presses `2` to select them triggering the inventory-opening failure state.

The coordinates and color for selected status in `mm_status.json` looks correct, so I'm struggling to understand how this could be failing at this point. *Why is the selected caster being reselected*?

## Bookcast Healing
Likewise, healing is failing:

```sh
heal skipped (all ≥ 99%)
```

Is reported when health percentages are *clearly* lower than 99% for *all three* players.

Both of these suggest some kind of **fundamental** error in this logic. 

## ADD: Status Display Only
Add a "dry" capture that does not re-detect statuses, but instead shows the status indicator rectangles and reports on the **current** status of each player -- selected or unselected, ready or busy, and current health percentage as measured on screen.

This will allow for better validation in the future when redetection is needed.

Bind the dry-run status display to `CTRL-ALT-0` for now.

## Deprecated Methods/Modules
In response to the last prompt, you said the following:

```
Old capture tools: util/capture-healthbar.py and the other old capture scripts still write the old format, so leave them unused.
```

Old capture tools need to be removed or relocated to `./deprecated`. I shouldn't have to remember to "leave them unused", and future AI agents *certainly* aren't going to know they're orphaned without spending a bunch of my compute/tokens figuring that out. Write a clearer Cursor rule so deprecated tools are treated properly and not left lying around like discarded trash...
