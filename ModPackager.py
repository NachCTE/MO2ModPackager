import json
import os
import zipfile

import mobase
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

CODE_EXT = (".esp", ".esm", ".esl", ".dll")
STORED_EXT = (".bsa", ".ba2", ".zip", ".7z", ".rar", ".png", ".jpg", ".mp3", ".ogg", ".xwm", ".fuz")  # already compressed
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


def add_tree(z, src, arc):
    for root, _, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            kind = zipfile.ZIP_STORED if f.lower().endswith(STORED_EXT) else zipfile.ZIP_DEFLATED
            z.write(full, arc + "/" + os.path.relpath(full, src).replace(os.sep, "/"), compress_type=kind)


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
                mobase.PluginSetting("list-replacers", "Also list mods without plugins/dlls (name + URL) in replacers.txt", True)]

    def display(self):
        out, _ = QFileDialog.getSaveFileName(self.w, "Export modpack", "modpack.zip", "Zip (*.zip)")
        if not out:
            return
        ml, manifest, replacers = self.o.modList(), [], []
        limit = int(self.o.pluginSetting(self.name(), "max-mod-mb"))
        only_active = bool(self.o.pluginSetting(self.name(), "only-active"))
        list_replacers = bool(self.o.pluginSetting(self.name(), "list-replacers"))
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
                if not code and not list_replacers:
                    continue
                full = code and not (limit and size / 2**20 > limit)
                manifest.append({"name": name, "url": mod.url(), "full": full, "enabled": active})
                if full:
                    add_tree(z, path, "mods/" + name)
                else:
                    replacers.append(f"{name}\t{mod.url()}")
            add_tree(z, self.o.overwritePath(), "configs/overwrite")
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
                add_tree(z, os.path.join(game, "enbseries"), "enbseries")
        QMessageBox.information(self.w, "Export", f"{len(manifest) - len(replacers)} mods packed, "
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
