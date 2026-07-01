"""Accessible editor for creating and editing soundscape definitions.

Manages the JSON library in ``soundscapes_dir()``; assigning a soundscape to a
story happens in the reader. Every control is named for screen readers and
slider/checkbox changes apply to the selected sound immediately.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import wx

from .. import portable
from . import library
from .model import Sound, Soundscape


class SoundscapeEditorDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Soundscape editor", size=(760, 560))
        self._current: Soundscape | None = None
        self._build_ui()
        self._reload_list()

    # ── UI ────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(self, label="&Soundscapes"), 0, wx.ALL, 4)
        self.sc_list = wx.ListBox(self)
        self.sc_list.SetName("Soundscapes")
        self.sc_list.Bind(wx.EVT_LISTBOX, self._on_select_soundscape)
        left.Add(self.sc_list, 1, wx.EXPAND | wx.ALL, 4)
        lrow = wx.BoxSizer(wx.HORIZONTAL)
        b_new = wx.Button(self, label="&New")
        b_del = wx.Button(self, label="De&lete")
        b_new.Bind(wx.EVT_BUTTON, self._on_new)
        b_del.Bind(wx.EVT_BUTTON, self._on_delete)
        lrow.Add(b_new, 0, wx.ALL, 2)
        lrow.Add(b_del, 0, wx.ALL, 2)
        left.Add(lrow, 0)
        root.Add(left, 0, wx.EXPAND)

        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(wx.StaticText(self, label="Na&me"), 0, wx.ALL, 4)
        self.name = wx.TextCtrl(self)
        self.name.SetName("Name")
        right.Add(self.name, 0, wx.EXPAND | wx.ALL, 4)
        right.Add(wx.StaticText(self, label="Master &volume"), 0, wx.ALL, 4)
        self.master = wx.Slider(self, value=80, minValue=0, maxValue=100)
        self.master.SetName("Master volume")
        right.Add(self.master, 0, wx.EXPAND | wx.ALL, 4)
        right.Add(wx.StaticText(self, label="&Reverb room size"), 0, wx.ALL, 4)
        self.reverb = wx.Slider(self, value=0, minValue=0, maxValue=100)
        self.reverb.SetName("Reverb room size")
        right.Add(self.reverb, 0, wx.EXPAND | wx.ALL, 4)

        right.Add(wx.StaticText(self, label="So&unds"), 0, wx.ALL, 4)
        self.sounds = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, col in enumerate(("Source", "Vol %", "Positional", "Azimuth")):
            self.sounds.InsertColumn(i, col)
        self.sounds.SetName("Sounds")
        self.sounds.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select_sound)
        right.Add(self.sounds, 1, wx.EXPAND | wx.ALL, 4)
        srow = wx.BoxSizer(wx.HORIZONTAL)
        b_add = wx.Button(self, label="&Add sound")
        b_rem = wx.Button(self, label="Re&move sound")
        b_add.Bind(wx.EVT_BUTTON, self._on_add_sound)
        b_rem.Bind(wx.EVT_BUTTON, self._on_remove_sound)
        srow.Add(b_add, 0, wx.ALL, 2)
        srow.Add(b_rem, 0, wx.ALL, 2)
        right.Add(srow, 0)

        prow = wx.BoxSizer(wx.HORIZONTAL)
        prow.Add(wx.StaticText(self, label="Sound vol"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        self.svol = wx.Slider(self, value=70, minValue=0, maxValue=100, size=(120, -1))
        self.svol.SetName("Sound volume")
        self.svol.Bind(wx.EVT_SLIDER, self._on_sound_edit)
        self.spos = wx.CheckBox(self, label="&Positional")
        self.spos.SetName("Positional")
        self.spos.Bind(wx.EVT_CHECKBOX, self._on_sound_edit)
        self.saz = wx.Slider(self, value=0, minValue=0, maxValue=359, size=(120, -1))
        self.saz.SetName("Azimuth")
        self.saz.Bind(wx.EVT_SLIDER, self._on_sound_edit)
        prow.Add(self.svol, 0, wx.ALL, 2)
        prow.Add(self.spos, 0, wx.ALL, 2)
        prow.Add(wx.StaticText(self, label="Azimuth"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        prow.Add(self.saz, 0, wx.ALL, 2)
        right.Add(prow, 0)

        brow = wx.BoxSizer(wx.HORIZONTAL)
        b_save = wx.Button(self, wx.ID_SAVE, "&Save")
        b_close = wx.Button(self, wx.ID_CLOSE, "&Close")
        b_save.Bind(wx.EVT_BUTTON, self._on_save)
        b_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        brow.Add(b_save, 0, wx.ALL, 4)
        brow.Add(b_close, 0, wx.ALL, 4)
        right.Add(brow, 0, wx.ALIGN_RIGHT)
        root.Add(right, 1, wx.EXPAND)
        self.SetSizer(root)

    # ── soundscape list ──────────────────────────────────────────
    def _reload_list(self) -> None:
        self.sc_list.Set(library.list_slugs())

    def _on_new(self, event) -> None:
        self._current = Soundscape("New soundscape")
        self._load_into_form()

    def _on_select_soundscape(self, event) -> None:
        i = self.sc_list.GetSelection()
        if i < 0:
            return
        sc = library.load(self.sc_list.GetString(i))
        if sc:
            self._current = sc
            self._load_into_form()

    def _on_delete(self, event) -> None:
        i = self.sc_list.GetSelection()
        if i < 0:
            return
        library.delete(self.sc_list.GetString(i))
        self._current = None
        self._reload_list()
        self.name.SetValue("")
        self.sounds.DeleteAllItems()

    # ── form ─────────────────────────────────────────────────────
    def _load_into_form(self) -> None:
        sc = self._current
        self.name.SetValue(sc.name)
        self.master.SetValue(int(sc.master_volume * 100))
        self.reverb.SetValue(int(sc.reverb_room_size * 100))
        self._refresh_sounds()

    def _refresh_sounds(self) -> None:
        self.sounds.DeleteAllItems()
        for s in (self._current.sounds if self._current else []):
            idx = self.sounds.InsertItem(self.sounds.GetItemCount(), s.source)
            self.sounds.SetItem(idx, 1, str(int(s.volume * 100)))
            self.sounds.SetItem(idx, 2, "yes" if s.positional else "no")
            self.sounds.SetItem(idx, 3, str(int(s.azimuth)))

    def _on_add_sound(self, event) -> None:
        if self._current is None:
            self._on_new(event)
        with wx.FileDialog(
            self, "Choose an ambient sound file",
            wildcard="Audio (*.ogg;*.wav;*.mp3;*.flac)|*.ogg;*.wav;*.mp3;*.flac",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            src = Path(dlg.GetPath())
        dest_dir = portable.sounds_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        try:
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
        except OSError as exc:
            wx.MessageBox(f"Couldn't copy sound: {exc}", "Soundscape", wx.OK | wx.ICON_ERROR, self)
            return
        self._current.sounds.append(Sound(source=src.name))
        self._refresh_sounds()

    def _on_remove_sound(self, event) -> None:
        i = self.sounds.GetFirstSelected()
        if i >= 0 and self._current:
            del self._current.sounds[i]
            self._refresh_sounds()

    def _on_select_sound(self, event) -> None:
        i = self.sounds.GetFirstSelected()
        if i < 0 or not self._current:
            return
        s = self._current.sounds[i]
        self.svol.SetValue(int(s.volume * 100))
        self.spos.SetValue(s.positional)
        self.saz.SetValue(int(s.azimuth))

    def _on_sound_edit(self, event) -> None:
        i = self.sounds.GetFirstSelected()
        if i < 0 or not self._current:
            return
        s = self._current.sounds[i]
        s.volume = self.svol.GetValue() / 100.0
        s.positional = self.spos.GetValue()
        s.azimuth = float(self.saz.GetValue())
        self._refresh_sounds()
        self.sounds.Select(i)

    def _on_save(self, event) -> None:
        if self._current is None:
            return
        self._current.name = self.name.GetValue().strip() or "Untitled"
        self._current.master_volume = self.master.GetValue() / 100.0
        self._current.reverb_room_size = self.reverb.GetValue() / 100.0
        library.save(self._current)
        self._reload_list()
        wx.MessageBox("Saved.", "Soundscape", wx.OK | wx.ICON_INFORMATION, self)
