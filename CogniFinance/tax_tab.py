# tax_tab.py
import os
import re
import logging
from datetime import date
from pathlib import Path
from decimal import Decimal
from typing import Dict, List, Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLabel, QComboBox, QListWidget, QListWidgetItem, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QAbstractItemView, QSizePolicy, QPushButton, QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt, QLocale, QCoreApplication
from PyQt6.QtGui import QFont

from ledger import GeneralLedger, ZERO
from tax_parser import (
    TaxTemplateLoader, TemplateValidationError, ResolvedTemplateSet,
    classify_base_recency, module_accepted_by_bases, localized, variant_choices,
    LEVELS, display_label, pretty_name,
)
from tax_format import format_money
from tax_profile import TaxProfileManager, record_selection, stored_layer, stored_module_enabled

TAX_TEMPLATES_DIR = Path.home() / "CogniSuite" / "TaxTemplates"
SHIPPED_DIR = TAX_TEMPLATES_DIR / "shipped"
USER_DIR = TAX_TEMPLATES_DIR / "user"
COMMUNITY_DIR = TAX_TEMPLATES_DIR / "community"

logger = logging.getLogger(__name__)


def _tier_info(tier_name: str):
    """(icon, tooltip) for a template's trust tier. Static, so it goes through
    QCoreApplication.translate rather than self.tr. Each call carries its own
    literal so Qt's extraction tool (pylupdate6) can find it."""
    if tier_name == "shipped":
        return "✅", QCoreApplication.translate("TaxTab", "Official template, current")
    if tier_name == "shipped_modified":
        return "⚠️", QCoreApplication.translate(
            "TaxTab", "This shipped file has been modified — reinstall the app to restore it"
        )
    if tier_name == "user_authored":
        return "✏️", QCoreApplication.translate("TaxTab", "Your template")
    if tier_name == "community":
        return "🌐", QCoreApplication.translate("TaxTab", "Community template")
    return "•", ""


class TaxTab(QWidget):
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger

        self.profile_manager = TaxProfileManager(ledger)
        # Last-used country / template layers / modules, restored below.
        self._ui_state = self.profile_manager.load_ui_state()
        self.loader = TaxTemplateLoader(
            shipped_dir=str(SHIPPED_DIR),
            user_dir=str(USER_DIR),
            community_dir=str(COMMUNITY_DIR),
        )

        self.current_set: ResolvedTemplateSet = None
        self.is_dark_mode = False
        # Language for template text that offers translations ("fr_CA" -> "fr" -> "en").
        self._lang = QLocale().name()

        # Filled by _refresh_template_lists()
        self._base_entries: List[dict] = []
        self._module_entries: List[dict] = []
        self._hidden_module_count = 0
        self._last_carryforwards: Dict[str, object] = {}

        self._init_ui()
        self._refresh_template_lists()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # ---------------------------------------------------------
        # 1. Template Selection (Prevents vertical stretching)
        # ---------------------------------------------------------
        template_box = QGroupBox(self.tr("1. Tax Template Selection"))
        # THIS IS THE FIX: Forces the box to only be as tall as its contents
        template_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        t_layout = QFormLayout()
        t_layout.setContentsMargins(8, 8, 8, 8) # Tight, even margins
        t_layout.setVerticalSpacing(2)           # Minimal space between rows

        self.country_combo = QComboBox()
        self.country_combo.currentIndexChanged.connect(self._on_country_changed)
        t_layout.addRow(self.tr("Country:"), self.country_combo)

        # One row per jurisdiction level (national / regional / local). A
        # row is hidden when the chosen country has no templates at that level.
        self.layer_labels: Dict[str, QLabel] = {}
        self.layer_combos: Dict[str, QComboBox] = {}
        for level in LEVELS:
            label = QLabel(self.tr("{label}:").format(label=self._default_level_label(level)))
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._on_layers_changed)
            self.layer_labels[level] = label
            self.layer_combos[level] = combo
            t_layout.addRow(label, combo)

        # Variant (e.g. filing status): shown only when a selected template offers one.
        self.variant_label = QLabel(self.tr("Filing status:"))
        self.variant_combo = QComboBox()
        self.variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        t_layout.addRow(self.variant_label, self.variant_combo)

        self.modules_list = QListWidget()
        self.modules_list.setFixedHeight(55) # Short, scrollable list
        self.modules_list.itemChanged.connect(self._on_template_changed)
        t_layout.addRow(self.tr("Modules:"), self.modules_list)

        self.status_label = QLabel(self.tr("Select templates to begin."))
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 11px;")
        t_layout.addRow(self.tr("Status:"), self.status_label)

        template_box.setLayout(t_layout)
        main_layout.addWidget(template_box)

        # ---------------------------------------------------------
        # 2. Bucket Totals & Section Breakdown (Also prevent stretching)
        # ---------------------------------------------------------
        buckets_box = QGroupBox(self.tr("2. Tax Calculation and Estimation: This estimator is for planning purposes only. Please consult a qualified tax professional."))
        buckets_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        b_layout = QVBoxLayout()
        b_layout.setContentsMargins(8, 8, 8, 8)
        b_layout.setSpacing(6)

        info_label = QLabel(self.tr("Summed totals for each tax bucket, broken down by profile section."))
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #6B7280; font-size: 11px;")
        b_layout.addWidget(info_label)

        self.bucket_tree = QTreeWidget()
        self.bucket_tree.setHeaderLabels([self.tr("Category / Section"), self.tr("Amount")])
        self.bucket_tree.setColumnCount(2)

        self.bucket_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.bucket_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.bucket_tree.setRootIsDecorated(True)
        self.bucket_tree.setAlternatingRowColors(True)
        self.bucket_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.bucket_tree.setMaximumHeight(250)

        b_layout.addWidget(self.bucket_tree)
        self.lbl_recon_gap = QLabel(self.tr("Ledger Income vs. Bucket Assigned: --"))
        self.lbl_recon_gap.setWordWrap(True)
        self.lbl_recon_gap.setStyleSheet("color: #6B7280; font-size: 11px; margin-top: 4px;")
        b_layout.addWidget(self.lbl_recon_gap)
        buckets_box.setLayout(b_layout)
        main_layout.addWidget(buckets_box)

        action_layout = QHBoxLayout()
        self.btn_edit_profile = QPushButton(self.tr("Edit Profile Section..."))
        self.btn_edit_profile.clicked.connect(self._open_profile_editor)
        action_layout.addWidget(self.btn_edit_profile)
        self.btn_export = QPushButton(self.tr("Export Tax Report (XLSX)"))
        self.btn_export.clicked.connect(self.export_report)
        action_layout.addWidget(self.btn_export)
        self.btn_save_cf = QPushButton(self.tr("Save Carry-forwards for Next Year"))
        self.btn_save_cf.setEnabled(False)
        self.btn_save_cf.clicked.connect(self._save_carryforwards)
        action_layout.addWidget(self.btn_save_cf)
        action_layout.addStretch()
        main_layout.addLayout(action_layout)

        # Push any remaining window space to the bottom
        main_layout.addStretch()

    @staticmethod
    def tooltip_for(t_data: dict) -> str:
        """
        Return the hover tooltip for a template entry, based on its
        trust tier. Empty string when the tier is unknown.
        """
        tier = t_data.get("tier")
        tier_name = tier.tier if tier else "user_authored"
        return _tier_info(tier_name)[1] if tier else (
            _tier_info("user_authored")[1]
        )

    # ------------------------------------------------------------------
    # Template selection: country -> one template per level -> modules
    # ------------------------------------------------------------------
    def _refresh_template_lists(self):
        available = self.loader.list_available_templates(lang=self._lang)
        accepted = available.get("accepted", [])
        self._base_entries = [t for t in accepted if t["template_type"] == "jurisdiction_base"]
        self._module_entries = [t for t in accepted if t["template_type"] == "income_module"]

        previous_country = self.country_combo.currentData() or self._ui_state.get("country")
        self.country_combo.blockSignals(True)
        try:
            self.country_combo.clear()
            names = {}
            for t in self._base_entries:
                j = t["jurisdiction"]
                if j.get("country_name") and j["country"] not in names:
                    names[j["country"]] = localized(j["country_name"], self._lang)
            for code in sorted({t["jurisdiction"]["country"] for t in self._base_entries}):
                self.country_combo.addItem(f"{code} — {names[code]}" if code in names else code, userData=code)
            idx = self.country_combo.findData(previous_country) if previous_country else -1
            if idx >= 0:
                self.country_combo.setCurrentIndex(idx)
        finally:
            self.country_combo.blockSignals(False)

        self._rebuild_layer_combos()
        self._rebuild_variant_combo()
        self._rebuild_modules_list()
        self._on_template_changed()

    def _on_country_changed(self, *_):
        self._rebuild_layer_combos()
        self._rebuild_variant_combo()
        self._rebuild_modules_list()
        self._on_template_changed()

    def _on_layers_changed(self, *_):
        self._rebuild_variant_combo()
        self._rebuild_modules_list()
        self._on_template_changed()

    def _on_variant_changed(self, *_):
        name = self.variant_combo.currentData()
        if name:
            self.profile_manager.set_variant(self._section_name(), name)
        self._on_template_changed()

    def _section_name(self) -> str:
        """Profile section for the selected country (each country keeps its own)."""
        country = self.country_combo.currentData()
        return self.profile_manager.resolve_section_name(country) if country else "Default"

    def _rebuild_variant_combo(self):
        selected = [t for t in self._base_entries if t["template_id"] in self._selected_base_ids()]
        heading, choices, default = variant_choices(selected, self._lang)
        combo = self.variant_combo
        previous = combo.currentData()
        combo.blockSignals(True)
        try:
            combo.clear()
            for name, label in choices:
                combo.addItem(label, userData=name)
            stored = self.profile_manager.get_variant(self._section_name()) if choices else None
            for candidate in (previous, stored, default):
                idx = combo.findData(candidate) if candidate else -1
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                    break
        finally:
            combo.blockSignals(False)
        self.variant_label.setVisible(bool(choices))
        combo.setVisible(bool(choices))
        if choices:
            self.variant_label.setText(
                self.tr("{label}:").format(label=heading or self.tr("Filing status"))
            )

    def _default_level_label(self, level: str) -> str:
        return {
            "national": self.tr("National"),
            "regional": self.tr("Regional"),
            "local": self.tr("Local"),
        }[level]

    def _level_label(self, level: str, entries: List[dict]) -> str:
        """Row label for a level: the template's own wording ("Province", "State", ...) if it has one."""
        for t in entries:
            custom = (t.get("jurisdiction") or {}).get("level_label")
            if custom:
                return localized(custom, self._lang, default=self._default_level_label(level))
        return self._default_level_label(level)

    def _rebuild_layer_combos(self):
        country = self.country_combo.currentData()
        for level in LEVELS:
            combo = self.layer_combos[level]
            label = self.layer_labels[level]
            entries = [
                t for t in self._base_entries
                if t["jurisdiction"]["country"] == country and t["jurisdiction"]["level"] == level
            ]
            previous = combo.currentData()
            combo.blockSignals(True)
            try:
                combo.clear()
                for t in entries:
                    combo.addItem(self._format_template_label(t), userData=t["template_id"])
                    combo.setItemData(combo.count() - 1, self.tooltip_for(t), Qt.ItemDataRole.ToolTipRole)
                # Only the outermost level is mandatory.
                if entries and level != "national":
                    combo.addItem(self.tr("(none)"), userData=None)
                idx = combo.findData(previous) if previous else -1
                wanted = stored_layer(self._ui_state, country, level)
                if idx < 0 and wanted:
                    idx = combo.findData(wanted)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                elif entries and level != "national" and wanted == "":
                    combo.setCurrentIndex(combo.count() - 1)   # the user chose (none) last time
                elif entries and level == "local":
                    combo.setCurrentIndex(combo.count() - 1)   # local defaults to (none)
            finally:
                combo.blockSignals(False)
            label.setVisible(bool(entries))
            combo.setVisible(bool(entries))
            if entries:
                label.setText(self.tr("{label}:").format(label=self._level_label(level, entries)))

    def _selected_base_ids(self) -> List[str]:
        ids = [self.layer_combos[level].currentData() for level in LEVELS]
        return [i for i in ids if i]

    def _rebuild_modules_list(self):
        """List only modules the selected bases accept (others could never load)."""
        selected = [t for t in self._base_entries if t["template_id"] in self._selected_base_ids()]
        previously = {
            self.modules_list.item(i).data(Qt.ItemDataRole.UserRole):
                self.modules_list.item(i).checkState()
            for i in range(self.modules_list.count())
        }
        self._hidden_module_count = 0
        self.modules_list.blockSignals(True)
        try:
            self.modules_list.clear()
            for t in self._module_entries:
                if not selected or not module_accepted_by_bases(selected, t):
                    self._hidden_module_count += 1 if selected else 0
                    continue
                item = QListWidgetItem(self._format_template_label(t))
                item.setData(Qt.ItemDataRole.UserRole, t["template_id"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                remembered = (
                    Qt.CheckState.Checked
                    if stored_module_enabled(self._ui_state, self.country_combo.currentData(), t["template_id"])
                    else Qt.CheckState.Unchecked
                )
                item.setCheckState(previously.get(t["template_id"], remembered))
                self.modules_list.addItem(item)
        finally:
            self.modules_list.blockSignals(False)

    def _format_template_label(self, t_data: dict) -> str:
        tier = t_data.get("tier")
        tier_name = tier.tier if tier else "user_authored"

        tier_icon, _tooltip = _tier_info(tier_name)
        tp = t_data.get("tax_period")
        year_info = tp.label if tp else (t_data.get("tax_year") or t_data.get("valid_from"))
        name = t_data.get("display_name", t_data["template_id"])
        return f"{tier_icon} {name} ({year_info})"

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    def _money(self, amount) -> str:
        """Format for the loaded set's currency using the user's locale."""
        loc = QLocale()
        return format_money(
            amount,
            self.current_set.currency if self.current_set else None,
            decimal_point=loc.decimalPoint(),
            group_separator=loc.groupSeparator(),
        )

    def _period_dates(self):
        tp = self.current_set.tax_period
        return tp.start, tp.end

    def _ledger_currency(self) -> Optional[str]:
        # The ledger doesn't record a currency yet; this activates the
        # mismatch guard as soon as it exposes one.
        return getattr(self.gl, "base_currency", None)

    def _save_selection(self):
        """Remember the current country / layer picks / module switches so the
        tab opens the way the user left it."""
        country = self.country_combo.currentData()
        if not country:
            return
        layers = {level: (self.layer_combos[level].currentData() or "") for level in LEVELS}
        modules = {}
        for i in range(self.modules_list.count()):
            item = self.modules_list.item(i)
            modules[item.data(Qt.ItemDataRole.UserRole)] = item.checkState() == Qt.CheckState.Checked
        self._ui_state = record_selection(self._ui_state, country, layers, modules)
        self.profile_manager.save_ui_state(self._ui_state)

    def _on_template_changed(self, *_):
        self._save_selection()
        base_ids = self._selected_base_ids()

        mod_ids = []
        for i in range(self.modules_list.count()):
            item = self.modules_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                mod_ids.append(item.data(Qt.ItemDataRole.UserRole))

        if not base_ids:
            self.status_label.setText(self.tr("Please select at least one jurisdiction template."))
            self.status_label.setStyleSheet("color: orange; font-size: 11px;")
            self.current_set = None
            self.bucket_tree.clear()
            return

        try:
            self.current_set = self.loader.load_layers(
                base_ids, mod_ids,
                ledger_currency=self._ledger_currency(),
                variant=self.variant_combo.currentData() if self.variant_combo.count() else None,
            )

            today = date.today()
            outdated = any(
                classify_base_recency(layer, today=today) == "outdated"
                for layer in self.current_set.layers
            )
            if outdated:
                self.status_label.setText("⚠️ " + self.tr("Base is outdated. Computes normally, but verify rates."))
                self.status_label.setStyleSheet("color: #d97706; font-weight: bold; font-size: 11px;")
            else:
                text = "✅ " + self.tr(
                    "Loaded {n} module(s) for tax year {period} ({currency})."
                ).format(
                    n=len(self.current_set.modules),
                    period=self.current_set.tax_period.label,
                    currency=self.current_set.currency,
                )
                if self._hidden_module_count:
                    text += " " + self.tr("{n} incompatible module(s) hidden.").format(
                        n=self._hidden_module_count
                    )
                self.status_label.setText(text)
                self.status_label.setStyleSheet("color: #059669; font-size: 11px;")

            self._populate_bucket_tree()
            self._run_reconciliation_check()

        except TemplateValidationError as e:
            self.current_set = None
            self.status_label.setText("❌ " + self.tr("Validation Error: {error}").format(error=e))
            self.status_label.setStyleSheet("color: #dc2626; font-weight: bold; font-size: 11px;")
            self.bucket_tree.clear()
        except BaseException as e:
            import traceback
            traceback.print_exc()
            self.current_set = None
            self.bucket_tree.clear()

    def _run_reconciliation_check(self):
        """
        Reconciliation check (income completeness): compare bucket-assigned
        income against the ledger's total income for the period, surface any gap.
        Scope: income only. Missing expense classifications don't trip this check.
        """
        # TODO: this check compares bucket-assigned income against the ledger's
        # total income. Currently sums only positive bucket values, which loses
        # loss-producing buckets (e.g. a rental property with a net loss this
        # year). Revisit when the first loss case is exercised -- likely needs to
        # sum all buckets that represent income-side activity rather than
        # filtering by sign.
        if not self.current_set:
            self.lbl_recon_gap.setText(self.tr("Ledger Income vs. Bucket Assigned: --"))
            return

        # The period is the loaded bases' tax period (calendar year unless
        # the template declares otherwise). No separate user-selectable year.
        start_date, end_date = self._period_dates()

        # 1. Ledger's total income for the period (Sum of all Revenue/Income accounts)
        ledger_total_income = self.gl.business_income_total(start_date, end_date)

        # 2. Bucket-assigned income
        # The category map UI is deferred, so we load the default section's map.
        section_data = self.profile_manager.load_section(self._section_name())
        category_map = section_data.get("category_map", {}) if section_data else {}

        # aggregate_by_category preserves normal-balance sign (Revenue = cr - dr = positive)
        bucket_totals = self.gl.aggregate_by_category(start_date, end_date, category_map)

        # Sum only positive bucket totals (income).
        bucket_assigned_income = sum((amt for amt in bucket_totals.values() if amt > ZERO), ZERO)

        # 3. Calculate and surface the gap
        gap = ledger_total_income - bucket_assigned_income

        if gap == ZERO:
            self.lbl_recon_gap.setText("✅ " + self.tr("Reconciliation: Perfect match ({amount})").format(
                amount=self._money(ledger_total_income)))
            self.lbl_recon_gap.setStyleSheet("color: #059669; font-size: 11px; font-weight: bold;")
        else:
            self.lbl_recon_gap.setText("⚠️ " + self.tr(
                "Reconciliation Gap: {gap} unassigned. (Ledger: {ledger} | Buckets: {buckets})"
            ).format(
                gap=self._money(gap),
                ledger=self._money(ledger_total_income),
                buckets=self._money(bucket_assigned_income),
            ))
            self.lbl_recon_gap.setStyleSheet("color: #d97706; font-size: 11px; font-weight: bold;")

    def _populate_bucket_tree(self):

        self.bucket_tree.clear()
        self._last_carryforwards = {}
        self.btn_save_cf.setEnabled(False)
        if not self.current_set:
            return

        from tax_calculator import TaxCalculator, ComputationBlockedError

        bold_font = QFont()
        bold_font.setBold(True)

        # ---- 1. Load the profile section ---------------------------------
        # One "Default" section per country; multi-section summing is deferred per the brief.
        section_data = self.profile_manager.load_section(self._section_name()) or {
            "category_map": {},
            "user_inputs": {},
            "skipped_inputs": [],
        }
        category_map = section_data.get("category_map", {})

        # Last year's carry-forwards arrive as inputs; anything the user has
        # entered themselves for the same field wins.
        carried = self.profile_manager.carryforward_inputs_for(
            self._section_name(), self.current_set.tax_year
        )
        if carried:
            section_data = dict(section_data)
            section_data["user_inputs"] = {**carried, **section_data.get("user_inputs", {})}

        # ---- 2. Aggregate each declared bucket ---------------------------
        start_date, end_date = self._period_dates()

        # Pass the full map once; aggregate_by_category returns one total
        # per category key. Buckets the category map doesn't reference come
        # back as 0.
        aggregates = self.gl.aggregate_by_category(start_date, end_date, category_map)

        # ---- 3. Compute --------------------------------------------------
        calc = TaxCalculator(self.current_set, section_data, aggregates)
        try:
            result = calc.compute()

        except ComputationBlockedError as e:
            # A required user_input field is blank and hasn't been skipped.
            # The full prompt-and-skip UI is deferred; surface the field name
            # and stop.
            self.status_label.setText(f"⚠️ {e}")
            self.status_label.setStyleSheet(
                "color: #d97706; font-weight: bold; font-size: 11px;"
            )
            return

        except Exception as e:
            # Any other computation failure -- surface it rather than a
            # half-populated tree.
            self.status_label.setText("❌ " + self.tr("Calculation error: {error}").format(error=e))
            self.status_label.setStyleSheet(
                "color: #dc2626; font-weight: bold; font-size: 11px;"
            )
            return

        # ---- 4. Render ---------------------------------------------------
        breakdown = result["breakdown"]

        # Section 1: per-bucket taxable contributions
        income_header = QTreeWidgetItem(self.bucket_tree)
        income_header.setText(0, self.tr("Income"))
        income_header.setFont(0, bold_font)

        for bucket_name in sorted(self.current_set.all_buckets.keys()):
            bucket = breakdown["buckets"].get(bucket_name, {})
            raw = bucket.get("raw", ZERO)
            taxable = bucket.get("taxable", ZERO)

            row = QTreeWidgetItem(income_header)
            row.setText(0, display_label(
                self.current_set.all_buckets.get(bucket_name), pretty_name(bucket_name), self._lang
            ))
            row.setText(1, self._money(raw))
            row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            if taxable != raw:
                sub = QTreeWidgetItem(row)
                sub.setText(0, self.tr("Taxable (after primitive)"))
                sub.setText(1, self._money(taxable))
                sub.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        income_header.setExpanded(True)

        # Section 2: pipeline totals
        totals_header = QTreeWidgetItem(self.bucket_tree)
        totals_header.setText(0, self.tr("Calculation"))
        totals_header.setFont(0, bold_font)

        def _add_total(parent, label, amount, bold=False):
            item = QTreeWidgetItem(parent)
            item.setText(0, label)
            item.setText(1, self._money(amount))
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if bold:
                item.setFont(0, bold_font)
                item.setFont(1, bold_font)
            return item

        _add_total(totals_header, self.tr("Total Income"), result["total_income"])
        _add_total(totals_header, self.tr("Less: Deductions"), breakdown["deductions"])
        _add_total(totals_header, self.tr("Net Income"), result["net_income"], bold=True)
        _add_total(totals_header, self.tr("Less: Pre Adjustments"), breakdown["pre_adjustments"])
        adjusted_row = _add_total(
            totals_header, self.tr("Adjusted Taxable Income"), result["adjusted_taxable_income"], bold=True
        )
        if breakdown.get("separate_income"):
            _add_total(adjusted_row, self.tr("of which taxed separately"), breakdown["separate_income"])
            adjusted_row.setExpanded(True)
        tax_row = _add_total(totals_header, self.tr("Income Tax Owed"), result["income_tax_owed"])
        # With more than one jurisdiction layer, show each layer's share.
        if len(self.current_set.layers) > 1:
            for layer in self.current_set.layers:
                level = layer["jurisdiction"]["level"]
                detail = breakdown["layers"].get(level, {})
                name = localized(layer.get("display_name"), self._lang, default=layer.get("template_id", level))
                _add_total(tax_row, name, detail.get("income_tax", ZERO))
            tax_row.setExpanded(True)
        # Note any alternative calculation (minimum tax, benefit cap, ...) that set the tax.
        for layer in self.current_set.layers:
            level = layer["jurisdiction"]["level"]
            for alt_name in breakdown["layers"].get(level, {}).get("alternatives_applied", []):
                alt_entry = next((a for a in layer.get("alternatives", []) if a.get("name") == alt_name), None)
                note = QTreeWidgetItem(tax_row)
                note.setText(0, self.tr("Alternative calculation applied: {name}").format(
                    name=display_label(alt_entry, pretty_name(alt_name), self._lang)))
                tax_row.setExpanded(True)
        _add_total(totals_header, self.tr("Less: Credits"), breakdown["credits_applied"])
        if breakdown.get("post_credit_adjustments"):
            _add_total(totals_header, self.tr("Plus: Post-Credit Charges"), breakdown["post_credit_adjustments"])
        if breakdown.get("refundable_credits"):
            _add_total(totals_header, self.tr("Less: Refundable Credits"), breakdown["refundable_credits"])
        _add_total(totals_header, self.tr("Tax After Credits"), result["tax_after_credits"], bold=True)

        # Levies as their own children
        levies_header = QTreeWidgetItem(totals_header)
        levies_header.setText(0, self.tr("Levies"))
        # A levy's label comes from its template entry; the breakdown is keyed by name
        # (with a " (level)" suffix if two layers reuse a name).
        levy_entries = {l.get("name"): l for l in self.current_set.all_levies}
        for levy_key, amount in breakdown.get("levies", {}).items():
            entry = levy_entries.get(levy_key) or levy_entries.get(levy_key.rsplit(" (", 1)[0])
            _add_total(levies_header, display_label(entry, levy_key, self._lang), amount)
        levies_header.setExpanded(True)

        _add_total(
            totals_header,
            self.tr("ESTIMATED NET REFUND") if result["total_tax"] < ZERO else self.tr("TOTAL ESTIMATED TAX"),
            abs(result["total_tax"]) if result["total_tax"] < ZERO else result["total_tax"],
            bold=True,
        )

        totals_header.setExpanded(True)

        # Amounts this calculation hands to next year.
        carry = result.get("carryforwards") or {}
        self._last_carryforwards = dict(carry)
        self.btn_save_cf.setEnabled(bool(carry))
        if carry:
            cf_labels = {
                cf["name"]: display_label(cf, pretty_name(cf["name"]), self._lang)
                for layer in self.current_set.layers for cf in layer.get("carryforwards") or []
            }
            cf_header = QTreeWidgetItem(self.bucket_tree)
            cf_header.setText(0, self.tr("Carry-forward to next year"))
            cf_header.setFont(0, bold_font)
            for name, amount in carry.items():
                _add_total(cf_header, cf_labels.get(name, pretty_name(name)), amount)
            cf_header.setExpanded(True)

        # ---- 5. Warnings (negative bucket clamps, etc.) ------------------
        warnings = breakdown.get("warnings", [])
        if warnings:
            warn_header = QTreeWidgetItem(self.bucket_tree)
            warn_header.setText(0, self.tr("Warnings"))
            warn_header.setFont(0, bold_font)
            for w in warnings:
                item = QTreeWidgetItem(warn_header)
                item.setText(0, w)
            warn_header.setExpanded(True)

    def refresh_summary(self):
        # Recompute using the currently selected template set.
        # This is what gets called when the ledger changes and the tab
        # needs to reflect new figures -- not when the templates themselves
        # need to be re-enumerated from disk.
        if self.current_set is not None:
            self._populate_bucket_tree()
            self._run_reconciliation_check()

    def export_report(self):
        """Exports the tax report to XLSX using the loaded template's tax year."""
        if not self.current_set:
            QMessageBox.warning(self, self.tr("No Template"), self.tr("Please select a valid template set first."))
            return

        # Per brief: the period comes from the loaded bases. No separate
        # user-selectable year.
        start_date, end_date = self._period_dates()
        period_label = re.sub(r"[^\w.-]+", "_", self.current_set.tax_period.label)

        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Export Tax Report"), f"Tax_Report_{period_label}.xlsx",
            self.tr("Excel Files (*.xlsx);;All Files (*)")
        )
        if not path:
            return
        if not path.endswith(".xlsx"):
            path += ".xlsx"

        # The category map UI is explicitly deferred per the brief.
        # We pass an empty map for now; the ledger will export income
        # and flat expenses without bucket categorization.
        category_map = {}
        medical_accounts = []

        try:
            self.gl.export_tax_report_xlsx(
                start_date, end_date, path,
                category_map=category_map,
                include_medical=True,
                medical_accounts=medical_accounts,
            )
            QMessageBox.information(self, self.tr("Export Complete"), self.tr("Tax report exported to:\n{path}").format(path=path))
        except Exception as e:
            QMessageBox.critical(self, self.tr("Export Failed"), str(e))

    def _save_carryforwards(self):
        if not self.current_set or not self._last_carryforwards:
            return
        ok = self.profile_manager.save_carryforwards(
            self._section_name(), self.current_set.tax_year, self._last_carryforwards
        )
        if ok:
            QMessageBox.information(
                self, self.tr("Carry-forwards Saved"),
                self.tr("Saved for the {period} tax year. They will be applied automatically to the following year.").format(
                    period=self.current_set.tax_period.label),
            )
        else:
            QMessageBox.warning(self, self.tr("Save Failed"), self.tr("Could not write the carry-forwards to the database."))

    def _open_profile_editor(self):
        if self.current_set is None:
            QMessageBox.information(
                self, self.tr("No Template Loaded"),
                self.tr("Load a template set before editing the profile.")
            )
            return

        from tax_profile_dialog import ProfileEditDialog

        dlg = ProfileEditDialog(
            ledger=self.gl,
            template_set=self.current_set,
            section_name=self._section_name(),
            parent=self,
        )
        if dlg.exec() == dlg.DialogCode.Accepted:
            # Profile changed -- recompute the tree using the new mapping.
            self._populate_bucket_tree()
            self._run_reconciliation_check()

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = is_dark
