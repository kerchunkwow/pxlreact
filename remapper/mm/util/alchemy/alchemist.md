# Alchemist
Implement `alchemist.py` as a companion module to the `remapper` utility for use during `Might and Magic` gameplay.

This extends the *remapping* concept and implements an

## Overview
`alchemist.py` should implement a basic idea: identify potions on screen by *color* and *position*, then use existing *recipe data* to quickly automate the creation of potions through the two basic alchemy interactions:

- `left-click` to *pick up* a potion
- `right-click` one potion into another to *mix* them

## Finding Potions
To mix potions, we will first need to *locate them* within the boundary of the *player inventory*. Finding potions can be achieved through pattern matching using reference images.

The project contains PNG files to assist in identifying potions within the player inventory. Potion colors are *very distinct*, but could be confused with *reagents* in the same inventory window, so err on the side of accuracy.

### Optimize Matching, Consider Precalculation
That being said, do **not** rely on a purely lazy "image match" algorithm that will be unnecessarily slow. Potions are distinct enough that it should be possible to create an optimized *fingerprint* of sorts for rapid matching. **Precalculate** if possible and store data for *re-use*.

### Use Position, Not Just Color
`alchemist.py` might be smarter and more accurate if it uses *position* to keep track of potions rather than relying on color/image matching alone.

For example, if the tool picks up a red potion and right-clicks on a blue potion, it can safely assume a *purple* potion now occupies the position previously held by the blue potion. Utilize this **if** doing so improves the design.

### Potion Images
`packs\mm8\alchemy` contains *seven* PNG images: one for each of the *six base potion colors* and `empty.png` for an empty potion bottle.

### Player Inventory
The player inventory is a grid of *126* squares in *14 columns* and *9 rows*. Each potion has a *width of 1* and a *height of 2*.

```py
"""
x,y coordinates of the top-left and bottom-right corners of the player inventory; all alchemy
components will be located within the rectangle bound by these points.
"""
inventory_top_y = 89
inventory_bottom_y = 963
inventory_left_x = 331
inventory_right_x = 1685
```

## Validate Offline
`packs\mm8\alchemy\sample-screen.png` holds a screenshot of a sample player inventory containing a diverse mix of potions, reagents, empty bottles, and empty space. Use this image to validate the algorithm.
