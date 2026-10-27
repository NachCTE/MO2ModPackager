import json
import os
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor

import mobase
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

CODE_EXT = (".esp", ".esm", ".esl", ".dll")
STORED_EXT = (".bsa", ".ba2", ".zip", ".7z", ".rar", ".png", ".jpg", ".mp3", ".ogg", ".xwm", ".fuz")  # already compressed
PARALLEL_MAX, BATCH = 16 * 2**20, 16  # bounds memory to ~256 MB in flight
GAME_FILES = ("enb", "skse64", "d3d11.dll", "d3dcompiler_46e.dll", "dxgi.dll")
PROFILE_CONFIGS = ("skyrim.ini", "skyrimprefs.ini", "skyrimcustom.ini")


def progress(parent, text, total):
    bar = QProgressDialog(text, None, 0, total, parent)
    bar.setWindowModality(Qt.WindowModality.WindowModal)
    bar.setMinimumDuration(0)
    return bar


def scan(path, need_size):
    """One pass: (has code files, size in bytes). Stops at the first code file if size isn't needed."""
    code, size = False, 0
    for root, _, files in os.walk(path):
        for f in files:
            code = code or f.lower().endswith(CODE_EXT)
            if need_size:
                size += os.path.getsize(os.path.join(root, f))
        if code and not need_size:
            break
    return code, size


def pack(item):
    full, name = item
    with open(full, "rb") as f:
        raw = f.read()
    c = zlib.compressobj(1, zlib.DEFLATED, -15)
    info = zipfile.ZipInfo.from_file(full, name, strict_timestamps=False)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info, c.compress(raw) + c.flush(), zlib.crc32(raw), len(raw)


def write_raw(z, info, data, crc, size):
    # shortcut: uses zipfile internals (stable 3.8-3.14) to write pre-compressed data; replace if a zipfile update breaks it
    info.CRC, info.file_size, info.compress_size, info.header_offset = crc, size, len(data), z.fp.tell()
    z.fp.write(info.FileHeader())
    z.fp.write(data)
    z.start_dir = z.fp.tell()
    z.filelist.append(info)
    z.NameToInfo[info.filename] = info


def add_tree(z, src, arc, pool, compress):
    """Small compressible files are deflated in parallel threads; everything else goes through z.write."""
    small = []
    for root, _, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            name = arc + "/" + os.path.relpath(full, src).replace(os.sep, "/")
            size = os.path.getsize(full)
            if compress and 0 < size <= PARALLEL_MAX and not f.lower().endswith(STORED_EXT):
                small.append((full, name))
            else:
                kind = zipfile.ZIP_DEFLATED if compress and not f.lower().endswith(STORED_EXT) else zipfile.ZIP_STORED
                z.write(full, name, compress_type=kind)
    for i in range(0, len(small), BATCH):
        for result in pool.map(pack, small[i:i + BATCH]):
            write_raw(z, *result)


def extract_tree(z, prefix, dest):
    for n in z.namelist():
        if n.startswith(prefix) and not n.endswith("/"):
            target = os.path.normpath(os.path.join(dest, n[len(prefix):]))
            if not target.startswith(os.path.normpath(dest)):
                continue  # zip-slip
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(n) as s, open(target, "wb") as d:
                d.write(s.read())


class Base(mobase.IPluginTool):
    title = ""

    def init(self, organizer):
        self.o = organizer
        return True

    def name(self): return "ModPackager " + self.title
    def author(self): return "Nacho"
    def description(self): return self.title + " modpack (mods with plugins/dlls, replacer list, configs)"
    def version(self): return mobase.VersionInfo(1, 0, 0)
    def settings(self): return []
    def displayName(self): return "Modpack/" + self.title
    def tooltip(self): return self.description()
    def icon(self): return QIcon()
    def setParentWidget(self, w): self.w = w


class Export(Base):
    title = "Export"

    def settings(self):
        return [mobase.PluginSetting("max-mod-mb", "Mods larger than this (MB) are listed instead of copied; 0 = no limit", 0),
                mobase.PluginSetting("only-active", "Export only mods enabled in the current profile", True),
                mobase.PluginSetting("compress", "Compress the zip (smaller but slower); off = store only", True),
                mobase.PluginSetting("export-replacers", "Also copy the files of mods without plugins/dlls (they are always listed in replacers.txt)", False)]

    def display(self):
        out, _ = QFileDialog.getSaveFileName(self.w, "Export modpack", "modpack.zip", "Zip (*.zip)")
        if not out:
            return
        ml, manifest, replacers = self.o.modList(), [], []
        limit = int(self.o.pluginSetting(self.name(), "max-mod-mb"))
        only_active = bool(self.o.pluginSetting(self.name(), "only-active"))
        export_replacers = bool(self.o.pluginSetting(self.name(), "export-replacers"))
        compress = bool(self.o.pluginSetting(self.name(), "compress"))
        pool = ThreadPoolExecutor(os.cpu_count())
        names = ml.allModsByProfilePriority()
        bar = progress(self.w, "Exporting...", len(names))
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1, strict_timestamps=False) as z:
            for i, name in enumerate(names):
                bar.setLabelText(name)
                bar.setValue(i)
                mod = ml.getMod(name)
                if mod.isSeparator() or mod.isBackup() or mod.isForeign() or name == "CC Content":
                    continue
                active = bool(ml.state(name) & mobase.ModState.ACTIVE)
                if only_active and not active:
                    continue
                path = mod.absolutePath()
                code, size = scan(path, bool(limit))
                full = (code or export_replacers) and not (limit and size / 2**20 > limit)
                manifest.append({"name": name, "url": mod.url(), "full": full, "enabled": active})
                if full:
                    add_tree(z, path, "mods/" + name, pool, compress)
                if not code or not full:
                    replacers.append(f"{name}\t{mod.url()}")
            add_tree(z, self.o.overwritePath(), "configs/overwrite", pool, compress)
            for f in os.listdir(self.o.profilePath()):
                if f.lower() in PROFILE_CONFIGS:
                    z.write(os.path.join(self.o.profilePath(), f), "configs/profile/" + f)
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            z.writestr("replacers.txt", "\n".join(replacers))
        bar.close()
        game = self.o.managedGame().gameDirectory().absolutePath()
        with zipfile.ZipFile(out[:-4] + "_game.zip", "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
            for f in os.listdir(game):
                if os.path.isfile(os.path.join(game, f)) and f.lower().startswith(GAME_FILES):
                    z.write(os.path.join(game, f), f)
            if os.path.isdir(os.path.join(game, "enbseries")):
                add_tree(z, os.path.join(game, "enbseries"), "enbseries", pool, compress)
        pool.shutdown()
        QMessageBox.information(self.w, "Export", f"{sum(m["full"] for m in manifest)} mods packed, "
                                                  f"{len(replacers)} replacers listed.\n"
                                                  "ENB/SKSE game files saved to the *_game.zip next to it.")


class Import(Base):
    title = "Import"

    def display(self):
        src, _ = QFileDialog.getOpenFileName(self.w, "Import modpack", "", "Zip (*.zip)")
        if not src:
            return
        missing, added = [], []
        with zipfile.ZipFile(src) as z:
            manifest = json.loads(z.read("manifest.json"))
            bar = progress(self.w, "Importing...", len(manifest))
            for i, m in enumerate(manifest):
                bar.setLabelText(m["name"])
                bar.setValue(i)
                if not m["full"]:
                    missing.append(f"{m['name']}  {m['url']}")
                elif not self.o.modList().getMod(m["name"]):  # shortcut: existing mods are never updated
                    mod = self.o.createMod(mobase.GuessedString(m["name"]))
                    extract_tree(z, "mods/" + m["name"] + "/", mod.absolutePath())
                    mod.setUrl(m["url"])
                    if m["enabled"]:
                        added.append(m["name"])
            extract_tree(z, "configs/overwrite/", self.o.overwritePath())
            extract_tree(z, "configs/profile/", self.o.profilePath())
        bar.close()
        self.o.refresh()
        for n in added:
            self.o.modList().setActive(n, True)
        QMessageBox.information(self.w, "Import", "Install these manually:\n\n" + "\n".join(missing))


class ImportGame(Base):
    title = "Import game files"

    def display(self):
        src, _ = QFileDialog.getOpenFileName(self.w, "Import ENB/SKSE game files", "", "Zip (*.zip)")
        if src:
            with zipfile.ZipFile(src) as z:
                extract_tree(z, "", self.o.managedGame().gameDirectory().absolutePath())
            QMessageBox.information(self.w, "Import", "Game files extracted.")


def createPlugins():
    return [Export(), Import(), ImportGame()]
