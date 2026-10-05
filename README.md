# BinGenerator

Local tool to change STM32CubeIDE firmware parameters from a form and get the `.bin`,
without opening the IDE.

## Start
Double-click **`BinGenerator.bat`**. Your browser opens on http://127.0.0.1:8765.
Requires Python 3 and STM32CubeIDE installed in `C:\ST` (its bundled GCC and make are used).

## Workflow
1. **Add project**: drop the project `.zip`, or give a folder path (a folder is always read live).
2. **Choose variables**: every `#define` and global variable initialisation found in your code
   (`Drivers/`, `Middlewares/` and `Debug/` are skipped). Click a row to see the code around it, tick it to put it
   on the settings screen. On the right you can set label, group, unit, min/max and the control type
   (number, text, string, checkbox 1/0, dropdown list, enable/disable a `#define`).
3. **Settings & Build**: edit values (changed ones are highlighted), optionally save/load **presets**
   (e.g. one per machine ID), then **Build .bin**.
4. The `.bin` lands in `output/<project>/` together with a `.json` listing exactly what was changed.

New version of the code? Use **Update source** with the new zip/folder — your chosen variables and presets are kept;
anything that disappeared from the code is flagged.

## How it builds
- Your original project is never modified. It is mirrored into `data/work/<project>/`, values are patched there.
- **Fast**: uses the project's own `Debug/makefile` (absolute paths from the PC that generated it are rewritten). ~20 s, then incremental.
- **CubeIDE regenerate**: runs `stm32cubeidec` headless to regenerate makefiles (used automatically when the project
  has no `Debug/` folder, e.g. from git). ~2 min.
