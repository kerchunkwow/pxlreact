# remapper: Project Overview
`remapper` is a subproject of `pxlreact` focused on creating a more simplified utility for direct aremapping of keys with minimal overhead.

`remapper` should inherit from `pxlreact` to avoid re-implementing existing functionality, but should implement its own framework for basic key substitution without the additional overhead that `pxl_remap.py` supplies for example.

## Use Case Context
The primary purpose of `remapper.py` is to allow the user to play oldschool DOS games, such as those available for purchase from GOG, while seamlessly redefining the control scheme without any need to mod the applications themselves. 

`remapper.py` will simply use a different mapping config for each game, replacing oldschool controls with more consistent modern standards (e.g., WASD sends arrows).

## Basic Remapping (Key Substitution)
`remapper.py` should utilize a simple substitution schema defined in an external config file, format of your choosing. The initial remapping scheme should provide the following:

Syntax follows: `key pressed -> key sent`

- w -> up
- a -> left
- d -> right
- s-> down

- f -> a
- r -> s
- e -> space
- space -> x
- c -> `
- b -> c
- ` -> F3
- tab -> m

There are a lot of "circular" binds here; make sure to account for this. The sent keys must be passed as-is to the application, not recaptured by `remapper.py`

## MVP Okay
`remapper.py` should be a much simpler application compared to `pxlreact.` IT is designed for singleplayer, offline games - generally RPGs and strategy games in which realtime, lightning-fast responses are unnecessary. This is really just a "key remapping" tool that happens to be built within the context of a more complex project.
