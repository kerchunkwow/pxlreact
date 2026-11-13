# ArcoMage - MM8
List of 11 locations to win ArcoMage in MM8 followed by MMExtension Lua Script for checking status:

## 11 Tavern Locations (Jadame)

> **Quest Giver:** Tonk Blueswan — *Blueswan Home*, Ravenshore (`x = 17330, y = 184`)

| # | Region | Tavern | House ID (`2DEvents`) | QBit | Start (Tower / Wall) | Start Income (B/G/D) | Victory (Tower / Res) | 1st Win Gold |
|---:|---|---|---:|---:|---|---|---|---|
| 1 | **Dagger Wound Islands** (Blood Drop) | The Grog and Grub | 107 | 153 | 15 / 5 | 2 / 2 / 2 | 30 / 100 | 600g |
| 2 | **Alvar** (Region / North) | Miho's Roadhouse | 108 | 154 | 10 / 20 | 3 / 1 / 2 | 125 / 350 | 2,000g |
| 3 | **Ravenshore** (City) | Kessel's Kantina | 109 | 155 | 20 / 5 | 2 / 2 / 2 | 50 / 150 | 1,000g |
| 4 | **Alvar** (City) | Profit House | 110 | 156 | 20 / 5 | 2 / 2 / 2 | 50 / 150 | 1,500g |
| 5 | **Ironsand Desert** (Rust) | Parched Throat | 111 | 157 | 25 / 10 | 3 / 3 / 3 | 75 / 200 | 1,500g |
| 6 | **Garrote Gorge** | Dragon's Blood Inn | 112 | 158 | 25 / 10 | 3 / 3 / 3 | 75 / 200 | 2,500g |
| 7 | **Shadowspire** (Twilight) | Black Company | 113 | 159 | 30 / 15 | 4 / 4 / 4 | 100 / 300 | 5,000g |
| 8 | **Murmurwoods** | Traveler's Rest | 114 | 160 | 30 / 15 | 4 / 4 / 4 | 100 / 300 | 3,500g |
| 9 | **Ravage Roaming** | Bull's Eye Inn | 115 | 161 | 20 / 10 | 5 / 5 / 5 | 150 / 400 | 4,000g |
| 10 | **Regna Island** (Harecksburg) | Pirate's Rest | 116 | 162 | 20 / 10 | 1 / 1 / 1 | 200 / 500 | 6,000g |
| 11 | **Ravenshore** (Smuggler's Cove) | The Dancing Ogre | 117 | 163 | 20 / 50 | 1 / 1 / 5 | 100 / 300 | 1,700g |

---

## MMExtension Lua Script (Check Status)

Press **`Ctrl + F1`** in-game to open the MMExtension Debug Console, paste the script below, and press **`Ctrl + Enter`** to run it.

```lua
local taverns = {
    { idx = 1,  house = 107, qbit = 153, region = "Dagger Wound Islands", name = "The Grog and Grub" },
    { idx = 2,  house = 108, qbit = 154, region = "Alvar (Roadhouse)",    name = "Miho's Roadhouse" },
    { idx = 3,  house = 109, qbit = 155, region = "Ravenshore",           name = "Kessel's Kantina" },
    { idx = 4,  house = 110, qbit = 156, region = "Alvar (City)",         name = "Profit House" },
    { idx = 5,  house = 111, qbit = 157, region = "Ironsand Desert",      name = "Parched Throat" },
    { idx = 6,  house = 112, qbit = 158, region = "Garrote Gorge",        name = "Dragon's Blood Inn" },
    { idx = 7,  house = 113, qbit = 159, region = "Shadowspire",          name = "Black Company" },
    { idx = 8,  house = 114, qbit = 160, region = "Murmurwoods",          name = "Traveler's Rest" },
    { idx = 9,  house = 115, qbit = 161, region = "Ravage Roaming",       name = "Bull's Eye Inn" },
    { idx = 10, house = 116, qbit = 162, region = "Regna Island",         name = "Pirate's Rest" },
    { idx = 11, house = 117, qbit = 163, region = "Ravenshore",           name = "The Dancing Ogre" },
}

local function is_won(t)
    -- Check Party.QBits (MM8 Arcomage quest flags 153..163)
    if Party.QBits and Party.QBits[t.qbit] then
        return true
    end
    -- Check Party.ArcomageWins (indexed by House ID 107..117 or 1..11 depending on MMExt version)
    if Party.ArcomageWins then
        local ok_h, v_h = pcall(function() return Party.ArcomageWins[t.house] end)
        if ok_h and v_h and v_h ~= 0 then return true end
        local ok_i, v_i = pcall(function() return Party.ArcomageWins[t.idx] end)
        if ok_i and v_i and v_i ~= 0 then return true end
    end
    -- Check MMMerge extra wins table if running MMMerge
    if vars and vars.ArcomageWinsExtra and vars.ArcomageWinsExtra[t.idx] then
        return true
    end
    return false
end

local won_count = 0
print("=== MM8 Arcomage Tournament Status ===")
for _, t in ipairs(taverns) do
    local won = is_won(t)
    if won then won_count = won_count + 1 end
    local status = won and "[WON]" or "[MISSING]"
    local house_name = (Game.HousesTxt and Game.HousesTxt[t.house] and Game.HousesTxt[t.house].Name) or t.name
    print(string.format("%2d. %-9s %-24s - %s (House %d, QBit %d)",
        t.idx, status, t.region, house_name, t.house, t.qbit))
end
print(string.format("=== Total: %d / %d ===", won_count, #taverns))
```

### One-Liner Console Alternatives
- **Dump raw `Party.ArcomageWins` table:**
  ```lua
  dump(Party.ArcomageWins)
  ```
- **Check only `QBits[153..163]`:**
  ```lua
  for q = 153, 163 do print(q, Party.QBits[q], Game.HousesTxt[q - 46].Name) end
  ```
