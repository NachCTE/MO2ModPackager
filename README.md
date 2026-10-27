# MO2 Mod Packager

Plugin de Mod Organizer 2 (2.5+) para exportar/importar una modlist de Skyrim.

- **Export**: zip con los mods que tienen `.esp/.esm/.esl/.dll` (`mods/`), `replacers.txt` (nombre + URL del resto), `manifest.json`, `overwrite` y los `.ini` del perfil. Los archivos ENB/SKSE de la carpeta del juego van en `*_game.zip` aparte.
- **Import** / **Import game files**: operaciones inversas.
- Opcion `max-mod-mb` (Settings > Plugins): mods mas pesados se listan en vez de copiarse.

## Instalacion

Copiar `ModPackager.py` a `<MO2>/plugins/` y reiniciar MO2. Menu: Tools > Modpack.

Nota: redistribuir archivos de mods de Nexus va contra sus normas; usar solo en privado.
