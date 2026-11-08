# Fix pixel-inspector

## Pixel Coordinates
There is something fundamentally wrong with `pixel-inspector.py` which is undermining its entire purpose: to facilitate easy and accurate identification of *pixel coordinates* and their *color values* currently on the screen.

When *live scanning* or inspecting pixels, the tool displays a grid of squares representing the pixels in a region below the mouse cursor; a magenta stroke identifies the pixel under the current cursor position (i.e., the center of the grid).

Once *frozen*, pixel data can be copied from the grid for use in other applications.

**The problem** is, that when *frozen*, the pixel coordinates reported by the GUI are **not consistent with the grid**. As I move my mouse within the GUI window, the reported coordinates are changing at positions that *do not correspond to visible grid boundaries*.

The inconsistency itself *may not the most critical issue*, but it suggests a *misalignment* in how the tool understands and communicates pixel positions, which is fundamental to the reason it exists. I need to be able to pinpoint precise coordinates of individual pixels without the need for downstream adjustments or guesswork.

What's going on, exactly? **Carefully** evaluate `pixel-inspector.py` and how its handling pixel coordinates; I question how this problem is even possible - it almost suggests the tool does not have a singular, consistent model for understanding pixel coordinates and translating them to the grid display.

## Config is Pointless, Confusing
While we're at it, can you improve the *configuration* portion of the module (`lines 28-57`)? It is *clogged* with what appears to be static globals and prior agents made no effort to explain their purpose or assign them meaningful names, making it doubly useless. 

I have no problem with static global values, just label them appropriately and separate them so they fall below configuration designed for *users* to make *manual adjustments to the behavior* of the tool.

Speaking of which, the *configuration I actually need is missing entirely*: `dimensions` of the grid and `size of individual squares` (i.e., how many pixels to display and how big to draw each one). Define `default values` matching the current settings, and mention any constraints that should be followed (e.g., odd-numbered sizes only).

Might as well make the font `face`, `size`, and `weight` for the pixel data display accessible here for confcig as well; I think current defaults are `8pt normal Consolas`.
