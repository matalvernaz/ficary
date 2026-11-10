"""Keep FFN directory navigation in the search window, with keyboard access and restorable history."""

import wx

from . import ffn_browse


class FFNBrowseMixin:
    def _init_ffn_browse(self):
        self._ffn_mode = "stories"
        self._ffn_history = []
        self._ffn_sort_by_view = {}
        self._ffn_current_view = ""
        self._ffn_fandom_path = ""
        self._ffn_fandom_label = ""
        self._ffn_general = False

    def _build_ffn_navigation(self, panel, sizer, pad):
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(panel, label="Look f&or:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.ffn_mode_ctrl = wx.Choice(panel, choices=["Stories", "Communities", "Categories"])
        self.ffn_mode_ctrl.SetSelection(0)
        self.ffn_mode_ctrl.SetName("Look for")
        self.ffn_mode_ctrl.Bind(wx.EVT_CHOICE, self._on_ffn_mode)
        row.Add(self.ffn_mode_ctrl, 0, wx.RIGHT, 8)
        self.ffn_back_btn = wx.Button(panel, label="&Back")
        self.ffn_back_btn.SetName("Back to previous FFN results")
        self.ffn_back_btn.Bind(wx.EVT_BUTTON, self._on_ffn_back)
        row.Add(self.ffn_back_btn, 0, wx.RIGHT, 8)
        self.ffn_open_btn = wx.Button(panel, label="Ope&n Selected")
        self.ffn_open_btn.SetName("Open selected category, fandom, or community")
        self.ffn_open_btn.Bind(wx.EVT_BUTTON, lambda event: self._open_ffn_result())
        row.Add(self.ffn_open_btn, 0, wx.RIGHT, 8)
        self.ffn_communities_btn = wx.Button(panel, label="Communities for Fan&dom")
        self.ffn_communities_btn.SetName("Browse communities for the selected category or fandom")
        self.ffn_communities_btn.Bind(wx.EVT_BUTTON, self._on_ffn_communities)
        row.Add(self.ffn_communities_btn, 0)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, pad)
        self.ffn_scope_ctrl = wx.StaticText(panel, label="Scope: all FFN stories")
        self.ffn_scope_ctrl.SetName("FFN browsing scope and results")
        sizer.Add(self.ffn_scope_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

    def _bind_ffn_navigation(self):
        self.filter_ctrls["category"].Bind(wx.EVT_CHOICE, self._on_ffn_category)
        for key in ("fandom", "community"):
            self.text_ctrls[key].Bind(wx.EVT_TEXT, self._on_ffn_scope_edit)
        self.query_ctrl.Bind(wx.EVT_TEXT, self._on_ffn_query_edit)
        self._refresh_ffn_controls()

    def _ffn_view(self):
        category = self.filter_ctrls["category"].GetStringSelection()
        fandom = self.text_ctrls["fandom"].GetValue().strip()
        query = self.query_ctrl.GetValue().strip()
        if self._ffn_mode == "categories":
            return "fandoms" if category != "any" else "categories"
        if self._ffn_mode == "communities":
            if query:
                return "community_search"
            if fandom or self._ffn_general:
                return "community_directory"
            return "community_fandoms" if category != "any" else "categories"
        if self.text_ctrls["community"].GetValue().strip():
            return "community_stories"
        return "stories"

    def _refresh_ffn_controls(self):
        view = self._ffn_view()
        sort_ctrl = self.filter_ctrls["sort"]
        if self._ffn_current_view:
            self._ffn_sort_by_view[self._ffn_current_view] = sort_ctrl.GetStringSelection()
        from .search import FFN_SORT
        choices = {
            "stories": list(FFN_SORT),
            "community_stories": list(ffn_browse.COMMUNITY_STORY_SORT),
            "categories": ["name"],
            "fandoms": list(ffn_browse.DIRECTORY_SORT),
            "community_fandoms": list(ffn_browse.COMMUNITY_FANDOM_SORT),
            "community_search": list(ffn_browse.COMMUNITY_SEARCH_SORT),
            "community_directory": list(ffn_browse.COMMUNITY_SORT),
        }[view]
        if view != self._ffn_current_view:
            sort_ctrl.SetItems(choices)
            sort_ctrl.SetSelection(0)
            saved_sort = self._ffn_sort_by_view.get(view, "")
            if not saved_sort and view == "community_stories":
                saved_sort = self._ffn_sort_by_view.get("stories", "")
            if saved_sort in choices:
                sort_ctrl.SetStringSelection(saved_sort)
        self._ffn_current_view = view

        # Retain story settings while browsing, but never send disabled filters to another endpoint.
        story_view = view in ("stories", "community_stories")
        community_story_filters = {"sort", "rating", "status", "genre", "min_words", "time", "match"}
        for key, ctrl in self.filter_ctrls.items():
            enabled = key in ("category", "sort")
            if view == "stories":
                enabled = True
            elif view == "community_stories":
                enabled = key in community_story_filters
            elif view == "community_directory":
                enabled = key in ("category", "sort", "language")
            elif view == "community_search":
                enabled = key in ("category", "sort", "match")
            ctrl.Enable(enabled)
        for key, ctrl in self.text_ctrls.items():
            if key == "community":
                ctrl.Enable(story_view)
            elif key == "fandom":
                ctrl.Enable(self._ffn_mode != "categories" and view != "community_stories")
            else:
                ctrl.Enable(view == "stories")
        for ctrl in self.checkbox_ctrls.values():
            ctrl.Enable(view == "stories")
        self._ffn_fandom_picker.Enable(self.text_ctrls["fandom"].IsEnabled())

        community = self.text_ctrls["community"].GetValue().strip()
        fandom = self.text_ctrls["fandom"].GetValue().strip()
        category = self.filter_ctrls["category"].GetStringSelection()
        if view == "community_stories":
            scope = f"stories in {community}"
        elif self._ffn_general and self._ffn_mode == "communities":
            scope = "general communities"
        elif self._ffn_mode != "categories" and fandom:
            scope = f"{self._ffn_mode} in {fandom}"
        elif category != "any":
            scope = f"{self._ffn_mode} in {ffn_browse.CATEGORIES[category][0]}"
        else:
            scope = f"all FFN {self._ffn_mode}"
        self.ffn_scope_ctrl.SetLabel(f"Scope: {scope}")

    def _ffn_filters(self, filters):
        filters["browse"] = self._ffn_mode
        # The first directory sort is meaningful, unlike the generic first "any" choice.
        filters["sort"] = self.filter_ctrls["sort"].GetStringSelection()
        if self._ffn_general and self._ffn_mode == "communities":
            filters["category"] = "general"
        if filters.get("fandom") == self._ffn_fandom_label:
            filters["fandom_path"] = self._ffn_fandom_path
            filters["fandom_label"] = self._ffn_fandom_label
        return filters

    def _ffn_state(self, state):
        self._ffn_sort_by_view[self._ffn_current_view] = self.filter_ctrls["sort"].GetStringSelection()
        state["ffn"] = {
            "mode": self._ffn_mode, "sorts": dict(self._ffn_sort_by_view),
            "fandom_path": self._ffn_fandom_path, "fandom_label": self._ffn_fandom_label,
            "general": self._ffn_general,
        }

    def _load_ffn_state(self, state):
        saved = state.get("ffn") or {}
        if not isinstance(saved, dict):
            return
        mode = saved.get("mode", "stories")
        self._ffn_mode = mode if mode in ("stories", "communities", "categories") else "stories"
        self.ffn_mode_ctrl.SetStringSelection(self._ffn_mode.title())
        sorts = saved.get("sorts")
        self._ffn_sort_by_view = {
            key: value for key, value in sorts.items() if isinstance(key, str) and isinstance(value, str)
        } if isinstance(sorts, dict) else {}
        # Older saved searches have a story sort but no per-view preferences.
        saved_sort = (state.get("filters") or {}).get("sort", "best match")
        self._ffn_sort_by_view.setdefault("stories", saved_sort if isinstance(saved_sort, str) else "best match")
        self._ffn_fandom_path = saved.get("fandom_path", "") if isinstance(saved.get("fandom_path", ""), str) else ""
        self._ffn_fandom_label = saved.get("fandom_label", "") if isinstance(saved.get("fandom_label", ""), str) else ""
        self._ffn_general = bool(saved.get("general", False))
        self._ffn_current_view = ""
        self._refresh_ffn_controls()

    def _ffn_snapshot(self):
        state = {
            "filters": {key: ctrl.GetStringSelection() for key, ctrl in self.filter_ctrls.items()},
            "text": {key: ctrl.GetValue() for key, ctrl in self.text_ctrls.items()},
            "checks": {key: ctrl.GetValue() for key, ctrl in self.checkbox_ctrls.items()},
        }
        self._ffn_state(state)
        return {
            "state": state, "query": self.query_ctrl.GetValue(),
            "raw": list(self._raw_results), "next_page": self.next_page,
            "last_query": self.last_query, "last_filters": dict(self.last_filters),
            "exhausted": self._upstream_exhausted, "selected": self.results_ctrl.GetFirstSelected(),
            "checked": self._checked_rows(),
        }

    def _clear_ffn_results(self):
        self.results_ctrl.DeleteAllItems()
        self.summary_ctrl.SetValue("")
        self.results = []
        self._raw_results = []
        self.last_query = None
        self.last_filters = {}
        self._upstream_exhausted = False
        self.apply_busy(bool(self.main_frame._downloading))

    def _on_ffn_mode(self, event):
        if self.main_frame._downloading:
            self.ffn_mode_ctrl.SetStringSelection(self._ffn_mode.title())
            return
        if self.last_query is not None:
            self._ffn_history.append(self._ffn_snapshot())
        self._ffn_mode = self.ffn_mode_ctrl.GetStringSelection().lower()
        self._ffn_general = False
        self._refresh_ffn_controls()
        self._clear_ffn_results()

    def _on_ffn_category(self, event):
        self._ffn_general = False
        self.text_ctrls["fandom"].ChangeValue("")
        self._ffn_fandom_path = ""
        self._ffn_fandom_label = ""
        self._refresh_ffn_controls()
        event.Skip()

    def _on_ffn_scope_edit(self, event):
        self._ffn_general = False
        self._refresh_ffn_controls()
        event.Skip()

    def _on_ffn_query_edit(self, event):
        self._refresh_ffn_controls()
        event.Skip()

    def _open_ffn_result(self, *, communities=False):
        if self.main_frame._downloading:
            return
        index = self.results_ctrl.GetFirstSelected()
        if index < 0 or index >= len(self.results):
            return
        row = self.results[index]
        kind = row.get("kind")
        if kind not in ("category", "general", "fandom", "community"):
            return
        self._ffn_history.append(self._ffn_snapshot())
        self.query_ctrl.ChangeValue("")
        self.text_ctrls["community"].ChangeValue("")
        target = "communities" if communities or self._ffn_mode == "communities" else "stories"
        self._ffn_general = kind == "general"
        if kind == "community":
            self._ffn_mode = "stories"
            self.text_ctrls["community"].ChangeValue(row["url"])
        elif kind == "general":
            self._ffn_mode = "communities"
            self.filter_ctrls["category"].SetSelection(0)
            self.text_ctrls["fandom"].ChangeValue("")
        else:
            self.filter_ctrls["category"].SetStringSelection(row["category"])
            self.text_ctrls["fandom"].ChangeValue("")
            if kind == "category":
                self._ffn_mode = "communities" if target == "communities" else "categories"
            else:
                self._ffn_mode = target
                self._ffn_fandom_label = row["title"]
                self._ffn_fandom_path = f"/{row['category']}/{row['slug']}/"
                self.text_ctrls["fandom"].ChangeValue(row["title"])
        self.ffn_mode_ctrl.SetStringSelection(self._ffn_mode.title())
        self._refresh_ffn_controls()
        self._on_search()

    def _on_ffn_communities(self, event):
        if self.main_frame._downloading:
            return
        index = self.results_ctrl.GetFirstSelected()
        if 0 <= index < len(self.results) and self.results[index].get("kind") in ("fandom", "category"):
            self._open_ffn_result(communities=True)
            return
        if not self.text_ctrls["fandom"].GetValue().strip():
            return
        self._ffn_history.append(self._ffn_snapshot())
        self._ffn_mode = "communities"
        self.ffn_mode_ctrl.SetStringSelection("Communities")
        self.query_ctrl.ChangeValue("")
        self._refresh_ffn_controls()
        self._on_search()

    def _on_ffn_back(self, event):
        if self.main_frame._downloading or not self._ffn_history:
            return
        previous = self._ffn_history.pop()
        state = previous["state"]
        for key, value in state["filters"].items():
            self.filter_ctrls[key].SetStringSelection(value)
        for key, value in state["text"].items():
            self.text_ctrls[key].ChangeValue(value)
        for key, value in state["checks"].items():
            self.checkbox_ctrls[key].SetValue(value)
        self.query_ctrl.ChangeValue(previous["query"])
        self._load_ffn_state(state)
        self.last_query = previous["last_query"]
        self.last_filters = previous["last_filters"]
        self._upstream_exhausted = False
        from .search import SearchPage
        self._populate_results(SearchPage(previous["raw"], exhausted=previous["exhausted"]), previous["next_page"], False)
        for index in previous["checked"]:
            self.results_ctrl.CheckItem(index, True)
        selected = previous["selected"]
        if 0 <= selected < len(self.results):
            self.results_ctrl.Select(0, False)
            self.results_ctrl.Focus(selected)
            self.results_ctrl.Select(selected)
            self.results_ctrl.EnsureVisible(selected)
        self.apply_busy(False)

    def _apply_ffn_busy(self, busy):
        self.ffn_mode_ctrl.Enable(not busy)
        self.ffn_back_btn.Enable(not busy and bool(self._ffn_history))
        index = self.results_ctrl.GetFirstSelected()
        selected = self.results[index] if 0 <= index < len(self.results) else {}
        directory = selected.get("kind") in ("category", "general", "fandom", "community")
        self.ffn_open_btn.Enable(not busy and directory)
        has_fandom = bool(self.text_ctrls["fandom"].GetValue().strip())
        self.ffn_communities_btn.Enable(not busy and (selected.get("kind") in ("category", "fandom") or has_fandom))
        if directory or (self.results and self.results[0].get("kind")):
            self.search_dl_btn.Disable()
            self.show_parts_btn.Disable()

    def _ffn_result_columns(self):
        filters = self.last_filters or {}
        mode = filters.get("browse", "stories")
        community_view = mode == "communities" and bool(self.last_query or filters.get("fandom") or filters.get("category") == "general")
        kind = self.results[0].get("kind") if self.results else None
        directory_view = mode in ("categories", "communities")
        if kind or (not self.results and directory_view):
            if kind == "community" or (not self.results and community_view):
                return [("Name", 300), ("Stories", 85), ("Followers", 85), ("Created", 95), ("Founder", 160), ("Staff", 70)]
            count = "Communities" if mode == "communities" else "Stories"
            return [("Name", 380), (count, 100)]
        from .gui_search import _SEARCH_COLUMNS
        return _SEARCH_COLUMNS

    @staticmethod
    def _ffn_result_cells(row, filters):
        if row["kind"] == "community":
            return [row.get(key, "") for key in ("title", "stories", "followers", "created", "founder", "staff")]
        count = "communities" if filters.get("browse") == "communities" else "stories"
        return [row["title"], row.get(count, "")]
