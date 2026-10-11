# Further mm6 Improvements
`capture-unselected.py` has done its job and bookcasts are working well, but before deleting it review the following. It may be a useful contextual reference for this more complex task.

## Generalize Player "Status" Data
Looking toward this next improvement, it's clear `mm6_unselected.json` is too narrow in scope, and a more generalized `mm6-status.json` is appropriate, where the (x,y) and color of unselected status is one component associated with each of the four player portraits in the MM6 user interface.

## Healthbar Data
Once generalized, the next property to add to each player data entry is related to the *healthbar*. Each portrait has a vertical healthbar to its right which indicates the portion of total health remaining for that character.

### Shared vs. Unique Properties
Each healthbar is identical in shape and appearance and they are all in the same y-position on the screen. A textual representation of this layout, where (#) represents each player's portrait and 'h' their healthbar: (1)h (2)h (3)h (4)h

The practical takeaway here is that healthbars share many properties: `height`, `width`, `max-y`, `min-y`, and notably also use the same color values to represent the presence or absence of health.

Therefore, the only truly unique property of each players healthbar is in its `x-coordinate` or horizontal positio.

### Capture Healthbar Logic
Similar to `capture-unselected.py`, a `capture-healthbar.py` module can be created for the one time "scrape" required to identify the position of each healthbar.

For `capture-healthbar.py` some extra unique logic can be added to verify functionality and capture robust data in a single pass. Once run, `capture-healthbar.py` should await the first mouseclick, and once received should respond as follows, using the position and color of the first click as a premise to guide a reliable discovery algorithm:

1. *Assume*: the first clicked location is in the brightly-colored horizontal midpoint of player 1's healthbar *and* that player 1 has *full health*
2. *Store the color* under the first click, understanding it represents healthy status
3. From the *x* coordinate of the click, scan *vertically* through successively greater and lesser *y* coordinates until a color is seen that differs from the original; the lowest value of *y* prior to the unmatched color reprsents the position of *maximum health*; the highest value of *y* prior to an unmatched color is the *minimum* possible health (the difference between these represents the height of the bar, which can later be used to calculate a % of remaining health)

Once you have the color and height of the first players healthbar, suggest an approach to discovering the other player's healthbar dimensions and using this color to create a simplified, "quickly identify % of remaining health for each player" algorithm.

